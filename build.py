# -*- coding: utf-8 -*-
"""
123Tube ビルダー（GitHub Actions 上で単体実行する自己完結版）

ローカルの scripts/yt_matome_*.py と違い、PCや水槽botのトークンに一切依存しない。
認証は環境変数 YT_API_KEY（GitHub Secrets）のAPIキーのみ。Python標準ライブラリだけで動く。

YouTube API Services 規約の遵守点:
  - 公式 Data API v3 のみ使用（HTMLスクレイピングは行わない）
  - 毎日このスクリプトが全データを取り直す＝「APIデータの保存は30日以内」を自動で満たす
  - 動画は youtube.com の視聴ページへリンクするだけ。サムネもGoogle側URLを直参照
  - サイト名に YouTube / YT を含めない（ブランドガイドライン準拠。名称は 123Tube）

クォータ: search.list=100u / videos.list=1u / mostPopular=1u
  → 検索21本 + 各種 = 約2,120u（1日上限10,000）
"""
import json, os, re, sys, html, datetime, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API_KEY = os.environ.get('YT_API_KEY', '').strip()
REGION, LIST_N = 'JP', 20
CHANNEL_ID = 'UCGkI3Cpu_a6yvizyqQLbKKA'          # うっちーPとエンタメの世界【大人の秘密基地】
SITE_URL = 'https://fabas-official.github.io/123tube/'

DATA = os.path.join(HERE, 'data.json')
HIST = os.path.join(HERE, 'history.json')
OWNCH = os.path.join(HERE, 'ownch_top.json')
INDEX = os.path.join(HERE, 'index.html')

# (キー, 表示名, [検索語...], 長さ絞り込み, 説明文)
# videoDuration で Shorts を除外している。理由=カードが16:9なので縦動画だと絵が崩れるため。
THEMES = [
    ('meshi',    '飯うま',     ['大食い 爆食', '飯テロ グルメ 食べ歩き'],        'medium',
     '大食い・爆食・グルメ。お腹が空く覚悟のある人だけどうぞ。'),
    ('kawaii',   'かわいい',   ['猫 犬 かわいい', '赤ちゃん 動物 癒やし'],       'medium',
     '犬・猫・赤ちゃん。無心で観たい時のための棚です。'),
    ('bikkuri',  'びっくり',   ['衝撃映像', '奇跡の瞬間'],                       'medium',
     '奇跡・衝撃・珍しい瞬間。思わず二度見するやつだけ。'),
    ('omoshiro', 'おもしろ',   ['爆笑 ドッキリ', 'ハプニング 珍事件'],           'medium',
     '爆笑・ドッキリ・珍事件。何も考えずに笑いたい日に。'),
    ('itai',     '痛い',       ['失敗 転倒 痛い', 'やらかし 失敗集'],            'medium',
     '見てるこっちが痛い、やらかしの記録。'),
    ('kane',     '借金・お金', ['借金 破産 貧乏', '節約 投資 失敗'],             'medium',
     'お金の失敗談から節約・投資まで。他人事じゃない話。'),
    ('sukatto',  'スカッと',   ['スカッとする話 逆転', '爆死 ガチャ ロスカット'], 'medium',
     '逆転・撃退・爆死。溜飲が下がる（or 下がらない）やつ。'),
    ('kandou',   '感動',       ['感動 泣ける 実話', '家族 奇跡 人助け'],          'medium',
     '泣きたい時は、泣いたほうがいい。'),
    ('kowai',    '怖い',       ['心霊 怪談', '都市伝説 ゾッとする話'],            'medium',
     '心霊・怪談・都市伝説。ひとりで観る勇気がある人向け。'),
    ('sugoi',    'すごい',     ['職人技 神業', '世界記録 才能'],                  'medium',
     '職人技・神業・世界記録。人間ってすごい。'),
    ('bgm',      '作業用BGM',  ['作業用BGM 集中'],                                'long',
     '手を止めずに流しっぱなしでどうぞ。'),
]
OWN_KEY, OWN_LABEL = 'ucchii', 'うっちーPの歌'
OWN_NOTE = 'このサイトを作っている人が書いた歌です。作詞はぜんぶ本人。'
# 「歌」判定に使う語。うっちーPの楽曲は「作詞：うっちー」表記が定型なのでこれを主軸にする。
SONG_HINT = ('作詞', 'Fabas', 'ファバス', 'オリジナル曲', 'MV', 'ミュージックビデオ', 'のうた', 'の歌')


# ---------------------------------------------------------------- API

def api(path, params):
    """Data API v3 をAPIキーで叩く。失敗はレスポンス本文ごと例外に載せる（silent fail防止）。"""
    p = dict(params)
    p['key'] = API_KEY
    url = 'https://www.googleapis.com/youtube/v3/' + path + '?' + urllib.parse.urlencode(p)
    try:
        with urllib.request.urlopen(url, timeout=45) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError('%s %s: %s' % (path, e.code, e.read().decode('utf-8', 'ignore')[:300]))


def parse_dur(iso):
    """ISO8601(PT1H2M3S) -> 1:02:03。不正値でも落とさない。"""
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso or '')
    if not m:
        return ''
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return '%d:%02d:%02d' % (h, mi, s) if h else '%d:%02d' % (mi, s)


def hydrate(ids):
    """IDリスト -> 実統計付きデータ。videos.list は50件までなので分割して呼ぶ。"""
    out = []
    for i in range(0, len(ids), 50):
        v = api('videos', {'part': 'snippet,statistics,contentDetails',
                           'id': ','.join(ids[i:i + 50]), 'maxResults': 50})
        for it in v.get('items', []):
            st = it.get('statistics', {})
            if 'viewCount' not in st:          # 再生数非公開はランキングに載せられない
                continue
            sn = it['snippet']
            th = sn.get('thumbnails', {})
            out.append({
                'videoId': it['id'], 'title': sn.get('title', ''),
                'channelTitle': sn.get('channelTitle', ''),
                'publishedAt': sn.get('publishedAt', '')[:10],
                'views': int(st['viewCount']),
                'comments': int(st.get('commentCount', 0)),
                'duration': parse_dur(it.get('contentDetails', {}).get('duration', '')),
                'thumb': (th.get('medium') or th.get('high') or th.get('default') or {}).get('url', ''),
            })
    return out


def theme_videos(queries, dur):
    """複数クエリをマージして重複を除き、再生数順で上位を返す。
    1語だけだと結果が偏るため、カテゴリごとに2クエリへ分けて広く拾う設計。"""
    ids = []
    for q in queries:
        s = api('search', {'part': 'id', 'type': 'video', 'order': 'viewCount', 'q': q,
                           'regionCode': REGION, 'relevanceLanguage': 'ja',
                           'videoDuration': dur, 'safeSearch': 'moderate', 'maxResults': 25})
        for i in s.get('items', []):
            vid = i.get('id', {}).get('videoId')
            if vid and vid not in ids:         # 同じ動画が2クエリに出るので重複除去
                ids.append(vid)
    vids = hydrate(ids)
    vids.sort(key=lambda x: -x['views'])       # videos.list は入力順を保証しないので必ず再ソート
    return vids[:LIST_N]


def trending():
    """総合タブ=今日の急上昇。mostPopular は1ユニットで済む公式エンドポイント。"""
    v = api('videos', {'part': 'snippet,statistics,contentDetails', 'chart': 'mostPopular',
                       'regionCode': REGION, 'maxResults': 30})
    return hydrate([i['id'] for i in v.get('items', [])])[:LIST_N]


def scan_own_channel():
    """チャンネル全動画を走査して歌の上位をキャッシュに書く（約250ユニット）。
    search.list に channelId+order=viewCount を渡すと直近分しか索引されず誤った結果になるため、
    uploads プレイリストを全ページ辿るのが唯一の正解ルート（2026-08-26 実測）。"""
    ch = api('channels', {'part': 'contentDetails', 'id': CHANNEL_ID})
    uploads = ch['items'][0]['contentDetails']['relatedPlaylists']['uploads']
    ids, seen, token, pages = [], set(), None, 0
    while True:
        p = {'part': 'contentDetails', 'playlistId': uploads, 'maxResults': 50}
        if token:
            p['pageToken'] = token
        r = api('playlistItems', p)
        for i in r.get('items', []):
            vid = i['contentDetails']['videoId']
            if vid not in seen:                # ページ跨ぎで同じIDが返ることがある
                seen.add(vid)
                ids.append(vid)
        token = r.get('nextPageToken')
        pages += 1
        if not token or pages > 200:           # 暴走ガード
            break
    vids = hydrate(ids)
    vids.sort(key=lambda x: -x['views'])
    songs = [v for v in vids if any(h in v['title'] for h in SONG_HINT)][:60]
    json.dump({'channelId': CHANNEL_ID, 'scanned': len(vids),
               'updated': datetime.date.today().isoformat(), 'songs': songs},
              open(OWNCH, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('own channel rescan: %d本 -> 歌%d本' % (len(vids), len(songs)))
    return songs


def own_songs():
    """うっちーPの歌トップ。曲の顔ぶれは滅多に変わらないのでキャッシュを使い、
    再生数だけ取り直す（2ユニット）。30日を超えたらフル再走査＝APIデータ30日ルールも満たす。"""
    songs, stale = [], True
    if os.path.exists(OWNCH):
        c = json.load(open(OWNCH, encoding='utf-8'))
        songs = c.get('songs', [])
        try:
            age = (datetime.date.today() -
                   datetime.date(*[int(x) for x in c.get('updated', '2000-01-01').split('-')])).days
            stale = age > 25                   # 30日ルールに余裕を持たせて25日で再走査
        except Exception:
            stale = True
    if stale or not songs:
        songs = scan_own_channel()
    fresh = hydrate([v['videoId'] for v in songs[:LIST_N]])
    fresh.sort(key=lambda x: -x['views'])
    return fresh[:LIST_N]


# ---------------------------------------------------------------- 描画

def big(n):
    """1,234,567 -> 123万回。日本語の桁感で一瞬で読めるようにする。"""
    if n >= 100000000:
        return str(round(n / 100000000.0, 1)) + '億回'
    if n >= 10000:
        return format(int(n / 10000), ',') + '万回'
    return format(n, ',') + '回'


def commentary(v):
    """再生数と経過日数から事実ベースの一言を生成する（推測は書かない）。"""
    try:
        y, m, dd = [int(x) for x in v['publishedAt'].split('-')]
        days = max(1, (datetime.date.today() - datetime.date(y, m, dd)).days)
    except Exception:
        days = 1
    yrs = days / 365.0
    span = (str(round(yrs, 1)) + '年') if yrs >= 1 else (str(days) + '日')
    s = '公開から' + span + 'で' + big(v['views']) + '。1日あたり約' + format(int(v['views'] / days), ',') + '回のペースです。'
    if v.get('delta'):
        s += ' 前日から+' + format(v['delta'], ',') + '回。'
    return s


def card(v):
    medal = ['gold', 'silver', 'bronze'][v['rank'] - 1]
    return ('<a class="card ' + medal + '" href="https://www.youtube.com/watch?v=' + html.escape(v['videoId']) +
            '" target="_blank" rel="noopener">'
            '<div class="tw"><img loading="lazy" src="' + html.escape(v['thumb']) + '" alt="">'
            '<span class="rk">' + str(v['rank']) + '</span>'
            '<span class="dur">' + html.escape(v['duration']) + '</span></div>'
            '<div class="mt"><p class="ch">' + html.escape(v['channelTitle']) + '</p>'
            '<h3>' + html.escape(v['title']) + '</h3>'
            '<p class="vw">' + big(v['views']) + '</p>'
            '<p class="cm">' + html.escape(commentary(v)) + '</p>'
            '<span class="btn">YouTubeで見る →</span></div></a>')


def row(v):
    delta = ('<span class="up">+' + format(v['delta'], ',') + '</span>') if v.get('delta') \
        else '<span class="flat">—</span>'
    return ('<a class="row" href="https://www.youtube.com/watch?v=' + html.escape(v['videoId']) +
            '" target="_blank" rel="noopener">'
            '<span class="n">' + str(v['rank']) + '</span>'
            '<span class="rtw"><img loading="lazy" src="' + html.escape(v['thumb']) + '" alt="">'
            '<i>' + html.escape(v['duration']) + '</i></span>'
            '<span class="ri"><b>' + html.escape(v['title']) + '</b>'
            '<em>' + html.escape(v['channelTitle']) + '</em></span>'
            '<span class="rs"><u>' + format(v['views'], ',') + '</u>'
            '<i>前日比 ' + delta + '</i><i>コメント ' + format(v['comments'], ',') + '</i></span></a>')


def render(d):
    tabs, panes = [], []
    for i, t in enumerate(d['themes']):
        on = ' on' if i == 0 else ''
        own = ' own' if t['key'] == OWN_KEY else ''
        tabs.append('<button class="tab' + on + own + '" data-t="' + t['key'] + '">' +
                    html.escape(t['label']) + '</button>')
        panes.append('<section class="pane' + on + '" id="p-' + t['key'] + '">'
                     '<div class="lead"><h2>' + html.escape(t['label']) + ' ベスト3</h2>'
                     '<p>' + html.escape(t['note']) + '</p></div>'
                     '<div class="top3">' + ''.join(card(v) for v in t['videos'][:3]) + '</div>'
                     '<h3 class="rh">' + html.escape(t['label']) + ' ランキング 4位〜' +
                     str(len(t['videos'])) + '位</h3>'
                     '<div class="rows">' + ''.join(row(v) for v in t['videos'][3:]) + '</div></section>')
    tpl = open(os.path.join(HERE, 'template.html'), encoding='utf-8').read()
    out = (tpl.replace('__TABS__', ''.join(tabs))
              .replace('__PANES__', ''.join(panes))
              .replace('__UPD__', d['updated']))
    open(INDEX, 'w', encoding='utf-8').write(out)
    return len(out.encode('utf-8'))


# ---------------------------------------------------------------- main

def main():
    if not API_KEY:
        print('FATAL: 環境変数 YT_API_KEY が未設定'); sys.exit(1)
    hist = json.load(open(HIST, encoding='utf-8')) if os.path.exists(HIST) else {}
    data = {'region': REGION, 'site': SITE_URL,
            # GitHub Actions は UTC で動くので、表示は日本時間に直す
            'updated': (datetime.datetime.now(datetime.timezone.utc) +
                        datetime.timedelta(hours=9)).strftime('%Y-%m-%d %H:%M'),
            'themes': []}
    newhist, failed = {}, []
    jobs = [('trend', '総合', None, None, '今日いちばん伸びている動画。毎日入れ替わります。')] + \
           list(THEMES) + [(OWN_KEY, OWN_LABEL, None, None, OWN_NOTE)]
    for key, label, q, dur, note in jobs:
        try:
            if key == 'trend':
                vids = trending()
            elif key == OWN_KEY:
                vids = own_songs()
            else:
                vids = theme_videos(q, dur)
        except Exception as e:                      # 1テーマ失敗で全体を落とさない
            print('NG %s: %s' % (label, str(e)[:160])); failed.append(label); continue
        if not vids:
            print('EMPTY %s' % label); failed.append(label); continue
        for r, v in enumerate(vids, 1):
            v['rank'] = r
            prev = hist.get(v['videoId'])           # 前回実行時との差＝前日比
            v['delta'] = (v['views'] - prev) if isinstance(prev, int) else None
            newhist[v['videoId']] = v['views']
        data['themes'].append({'key': key, 'label': label, 'note': note, 'videos': vids})
        print('OK %s (%d)' % (label, len(vids)))

    # 全滅した場合は既存の index.html を壊さずに異常終了させる（空サイトの公開を防ぐ）
    if len(data['themes']) < 3:
        print('FATAL: 取得できたテーマが%d件しかない。既存サイトを維持して中断' % len(data['themes']))
        sys.exit(1)

    json.dump(data, open(DATA, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(newhist, open(HIST, 'w', encoding='utf-8'), ensure_ascii=False)
    size = render(data)
    print('DONE themes=%d videos=%d bytes=%d failed=%s' % (
        len(data['themes']), sum(len(t['videos']) for t in data['themes']), size, failed or 'なし'))


if __name__ == '__main__':
    main()
