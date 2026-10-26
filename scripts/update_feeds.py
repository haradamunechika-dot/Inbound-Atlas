#!/usr/bin/env python3
"""訪日アトラス：ニュースと為替を自動更新する（GitHub Actions から1日3回実行）。

- data/news.json      : Googleニュースのキーワード検索RSS（全国＋47都道府県）。見出しは原文のまま。
- data/fx_monthly.json : 三菱UFJ銀行の月中平均（TTSとTTBの中間値）。新しい月が公表されたら追記。

取得に失敗したものは前回のファイルをそのまま残す（空のデータで上書きしない）。
標準ライブラリのみで動く。
"""
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
JST = timezone(timedelta(hours=9))
NOW = datetime.now(JST)
UA = 'InboundAtlasFeedBot/1.0 (+https://github.com/haradamunechika-dot/Inbound-Atlas)'

PREFS = ['北海道', '青森県', '岩手県', '宮城県', '秋田県', '山形県', '福島県', '茨城県', '栃木県', '群馬県', '埼玉県', '千葉県',
         '東京都', '神奈川県', '新潟県', '富山県', '石川県', '福井県', '山梨県', '長野県', '岐阜県', '静岡県', '愛知県', '三重県',
         '滋賀県', '京都府', '大阪府', '兵庫県', '奈良県', '和歌山県', '鳥取県', '島根県', '岡山県', '広島県', '山口県', '徳島県',
         '香川県', '愛媛県', '高知県', '福岡県', '佐賀県', '長崎県', '熊本県', '大分県', '宮崎県', '鹿児島県', '沖縄県']
NATIONAL_QUERIES = ['インバウンド 観光', '訪日外国人 観光', '訪日客', '観光庁 インバウンド', 'JNTO 訪日外客数']

# 見出しに観光・インバウンドの語がない記事は外す
RELEVANT = re.compile(r'インバウンド|訪日(客|外国人|外客|旅行|観光|需要|市場)|外国人(観光|旅行|客|宿泊|向け)|観光|旅行|宿泊|ホテル|旅館|民泊|クルーズ|空港|就航|航空|路線|免税|ツアー|誘客|誘致|DMO|来日客')
# 政治・外交などの記事を外す（「首相が訪日」など）
EXCLUDE = re.compile(r'首相|大統領|外相|首脳|防衛|安保|選挙|国会|与党|野党|移民|不法|在留資格|株価|決算短信')
# 見出しの語句による自動分類（上から順に最初に当てはまったもの）
CATEGORIES = [
    ('オーバーツーリズム', r'オーバーツーリズム|観光公害|混雑|マナー|二重価格'),
    ('統計', r'万人|統計|過去最|前年|前月|推計|調査結果|[0-9０-９.．]+[%％]'),
    ('安全', r'地震|台風|事故|災害|クマ|避難|安全|警報|事件|死亡'),
    ('航空', r'空港|就航|航空|国際線|直行便|定期便|LCC|運航|増便|減便|運休'),
    ('クルーズ', r'クルーズ|寄港|客船'),
    ('宿泊', r'ホテル|旅館|宿泊|民泊|開業|応援割'),
    ('消費', r'消費|免税|買い物|売上|購入|百貨店'),
    ('交通', r'鉄道|JR|新幹線|バス|タクシー|交通|列車|モビリティ'),
    ('政策', r'観光庁|政府|国交省|知事|市長|条例|税|規制|制度|補助|予算|議会|公募|プロポーザル'),
    ('プロモーション', r'PR|プロモーション|誘客|誘致|商談|キャンペーン|発信|招請|ファムトリップ|取材|集客'),
    ('イベント', r'祭|イベント|大会|フェス|展示|博覧会|万博|講座|セミナー'),
    ('体験', r'体験|ツアー|アクティビティ|文化|料理|グルメ|食'),
]
NEWS_DAYS = 30          # 収録する期間（日）
PER_PREF = 6            # 都道府県ごとの上限
NATIONAL_MAX = 30       # 全国の上限


def fetch(url, encoding='utf-8', tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode(encoding, 'replace')
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def write_json(name, obj):
    path = os.path.join(DATA, name)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write('\n')
    os.replace(tmp, path)


def read_json(name, default):
    try:
        with open(os.path.join(DATA, name), encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


# ---------------- news ----------------
def rss(query):
    q = urllib.parse.quote(query + ' when:14d')
    xml = fetch(f'https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja')
    root = ET.fromstring(xml)
    out = []
    for it in root.iter('item'):
        title = html.unescape((it.findtext('title') or '').strip())
        src_el = it.find('source')
        source = (src_el.text or '').strip() if src_el is not None else ''
        if source and title.endswith(' - ' + source):
            title = title[: -len(' - ' + source)].strip()
        title = re.sub(r'^[＜<]画像\s*\d+\s*/\s*\d+[＞>]\s*', '', title)
        link = (it.findtext('link') or '').strip()
        try:
            dt = email.utils.parsedate_to_datetime(it.findtext('pubDate')).astimezone(JST)
        except Exception:  # noqa: BLE001
            continue
        if not title or not link.startswith('https://') or dt < NOW - timedelta(days=NEWS_DAYS):
            continue
        out.append({'dt': dt, 'title': title, 'source': source or '情報源不明', 'url': link})
    return out


def category(title):
    for name, pat in CATEGORIES:
        if re.search(pat, title):
            return name
    return 'その他'


def prefs_in(title):
    """見出しに出てくる都道府県（正式名か「県・府・都」を除いた名前）。"""
    t = title.replace('東京都', '東京')
    found = []
    for p in PREFS:
        short = p if p == '北海道' else p[:-1]
        if p in title or short in t:
            found.append(p)
    return found


def update_news():
    items = {}  # 見出しで重複をまとめる

    def add(it, scope, hint):
        title = it['title']
        if not RELEVANT.search(title) or EXCLUDE.search(title):
            return
        key = re.sub(r'\s+', '', title)
        cur = items.get(key)
        if cur is None:
            cur = dict(it, scope=scope, prefs=prefs_in(title), hints=[])
            items[key] = cur
        elif it['dt'] > cur['dt']:
            cur.update({k: it[k] for k in ('dt', 'source', 'url')})
        if hint and hint not in cur['hints']:
            cur['hints'].append(hint)
        if scope == 'regional' or cur['prefs']:
            cur['scope'] = 'regional'

    ok = 0
    for q in NATIONAL_QUERIES:
        try:
            for it in rss(q):
                add(it, 'national', None)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print('news national failed:', q, e, file=sys.stderr)
        time.sleep(1)
    per_pref = {}
    for p in PREFS:
        try:
            got = sorted(rss(f'"{p}" (インバウンド OR 観光客 OR 訪日)'), key=lambda x: x['dt'], reverse=True)
            n = 0
            for it in got:
                if n >= PER_PREF:
                    break
                if RELEVANT.search(it['title']) and not EXCLUDE.search(it['title']):
                    add(it, 'regional', p)
                    n += 1
            per_pref[p] = n
            ok += 1
        except Exception as e:  # noqa: BLE001
            print('news pref failed:', p, e, file=sys.stderr)
        time.sleep(1)
    if ok < (len(NATIONAL_QUERIES) + len(PREFS)) // 2:
        print('news: too many failures, keeping previous file', file=sys.stderr)
        return False
    rows = sorted(items.values(), key=lambda x: x['dt'], reverse=True)
    nat = [r for r in rows if r['scope'] == 'national'][:NATIONAL_MAX]
    reg = [r for r in rows if r['scope'] == 'regional']
    out = []
    for i, r in enumerate(sorted(nat + reg, key=lambda x: x['dt'], reverse=True), 1):
        out.append({'date': r['dt'].strftime('%Y-%m-%d'), 'title': r['title'], 'source': r['source'], 'url': r['url'],
                    'sentiment': None, 'category': category(r['title']), 'prefs': '・'.join(r['prefs']),
                    'search_prefs': '・'.join(r['hints']),
                    'scope': r['scope'], 'id': i})
    if len(out) < 20:
        print('news: too few rows, keeping previous file', file=sys.stderr)
        return False
    prev = read_json('news.json', {})
    if isinstance(prev, dict) and prev.get('rows') == out:
        return False
    write_json('news.json', {'updated_at': NOW.strftime('%Y-%m-%dT%H:%M:%S+09:00'),
                             'updated_label': NOW.strftime('%Y年%-m月%-d日 %H:%M'),
                             'source': 'Googleニュース（キーワード検索のRSS）', 'rows': out})
    print(f'news: {len(out)} rows (national {len(nat)}, regional {len(reg)})')
    return True


# ---------------- fx ----------------
FX = {  # 通貨: (市場, 通貨名, 単位)
    'USD': ('米国', '米ドル', 1), 'EUR': ('欧州（独仏伊西など）', 'ユーロ', 1), 'GBP': ('英国', '英ポンド', 1),
    'AUD': ('オーストラリア', '豪ドル', 1), 'HKD': ('香港', '香港ドル', 1), 'SGD': ('シンガポール', 'シンガポール・ドル', 1),
    'CNY': ('中国', '人民元', 1), 'THB': ('タイ', 'タイ・バーツ', 1), 'KRW': ('韓国', '韓国ウォン', 100),
}


def month_avg(ym):
    page = fetch(f'https://www.murc-kawasesouba.jp/fx/monthend/index.php?id={ym[2:4]}{ym[5:7]}', encoding='euc_jp')
    res = {}
    for tr in re.findall(r'<tr.*?</tr>', page, flags=re.S):
        c = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', x)).strip() for x in re.findall(r'<t[dh][^>]*>.*?</t[dh]>', tr, flags=re.S)]
        if len(c) >= 8 and c[2] in FX:
            try:
                tts, ttb = float(c[6]), float(c[7])
            except ValueError:
                continue
            unit = int(re.sub(r'\D', '', c[3]) or 1)
            if unit != FX[c[2]][2]:
                continue
            res[c[2]] = round((tts + ttb) / 2 + 1e-9, 2)
    return res


def update_fx():
    rows = read_json('fx_monthly.json', [])
    if not rows:
        return False
    have = {r['ym'] for r in rows}
    last = max(have)
    y, m = int(last[:4]), int(last[5:])
    changed = False
    for _ in range(3):  # 追いつくまで最大3か月分
        y, m = (y + (m == 12), m % 12 + 1)
        ym = f'{y:04d}-{m:02d}'
        if ym >= NOW.strftime('%Y-%m'):
            break  # 当月の月中平均は月が終わるまで出ない
        try:
            avg = month_avg(ym)
        except Exception as e:  # noqa: BLE001
            print('fx failed:', ym, e, file=sys.stderr)
            break
        if len(avg) < len(FX):
            print('fx: not published yet', ym, sorted(avg), file=sys.stderr)
            break
        for cur, (market, name, unit) in FX.items():
            rows.append({'currency': cur, 'currency_name': name, 'market': market, 'unit': unit, 'ym': ym, 'yen_per_unit': avg[cur]})
        changed = True
        print('fx: added', ym)
    if changed:
        write_json('fx_monthly.json', rows)
    return changed


if __name__ == '__main__':
    os.makedirs(DATA, exist_ok=True)
    a = update_news()
    b = update_fx()
    print('changed:', a or b)
