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

観られない動画をランキングに載せない仕組み（2026-08-26 内田さん指摘で追加）:
  - 削除・非公開になった動画は videos.list のレスポンスから消えるため自動で落ちる
  - 消えないのに日本では観られないもの（年齢制限 / regionRestriction で日本がブロック）は
    unplayable() で明示的に弾く
  - 弾いた分は再生数順に並べ直したあと上位を切るので、**下位が自動で繰り上がる**
  - 除外が起きた日は件数と理由を必ずログに出す（握り潰さない）

クォータ: search.list=100u / videos.list=1u / mostPopular=1u
  → 検索21本 + 各種 = 約2,120u（1日上限10,000）
  ⚠️ search.list には総合10,000とは**別枠の日次上限**('Search Queries per day')がある。
     ここが先に尽きると chart=mostPopular は通るのに search だけ429になる。
     期間指定の補完は FALLBACK_MAX 件までに制限してこれを守る。
"""
import json, os, re, sys, html, datetime, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API_KEY = os.environ.get('YT_API_KEY', '').strip()
REGION, LIST_N = 'JP', 20
# ジャンルタブが「歴代ランキング」になって中身が入れ替わらない問題への対処。
# この日数以内に公開された動画の中での再生数順にする（2026-08-26 追加）。
# 広げるほど殿堂入り寄り・狭めるほど日替わり寄りになる。ここ1箇所で調整できる。
FRESH_DAYS = 90
FALLBACK_MAX = 2        # 期間指定なしでの補完を許す最大テーマ数（検索の日次別枠を守るため）
_fallback_used = 0      # 1回のビルド内で使った補完回数
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

# 各タブの先頭に固定表示する「まず観る1本」。再生数ランキングとは切り離して置く＝
# ランキングの数値を偽らずに、入口となる動画を確実に見せるための枠。
# ucchii: Fabasの誕生秘話＋ベスト曲を6分31秒にまとめた公式ダイジェスト（内田さん指定 2026-08-26）
PINNED = {
    'ucchii': {
        'videoId': 'cWzRARpo7nQ',
        'lead': 'まずはこの1本',
        'note': 'Fabasの成り立ちと代表曲が、6分半で全部わかります。ここから聴き始めるのがいちばん早いです。',
    },
}
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


def unplayable(it):
    """日本の閲覧者が「この動画は利用できません」に当たる動画を弾く。理由文字列 or None を返す。

    削除・非公開になった動画は videos.list のレスポンスから消えるので自動で落ちる。
    ここで見るのは **消えないのに日本では見られない** ケース（2026-08-26 内田さん指摘で追加）。

    ⚠️ 判定は「明確な証拠があるときだけ弾く」設計にしている。
       フィールドが欠けているだけで弾くと、API仕様変更でランキングが空になり
       main() の部分失敗ガードに引っかかってサイトが更新されなくなる（＝守りすぎて壊す）。
    """
    stat = it.get('status', {})
    if stat.get('privacyStatus') == 'private':
        return 'private'
    if stat.get('uploadStatus') in ('rejected', 'failed', 'deleted'):
        return 'upload:' + stat['uploadStatus']

    cd = it.get('contentDetails', {})
    # 年齢制限はログインの壁が出て、そのままでは観られない
    if cd.get('contentRating', {}).get('ytRating') == 'ytAgeRestricted':
        return 'age-restricted'

    rr = cd.get('regionRestriction', {})
    if REGION in (rr.get('blocked') or []):
        return 'blocked:' + REGION
    allowed = rr.get('allowed')
    if allowed is not None and REGION not in allowed:
        return 'not-allowed:' + REGION
    return None


def hydrate(ids):
    """IDリスト -> 実統計付きデータ。videos.list は50件までなので分割して呼ぶ。

    videos.list のクォータは part の数に関係なく 1ユニット固定なので、
    status を足しても消費は増えない。
    """
    out, dropped = [], []
    for i in range(0, len(ids), 50):
        v = api('videos', {'part': 'snippet,statistics,contentDetails,status',
                           'id': ','.join(ids[i:i + 50]), 'maxResults': 50})
        for it in v.get('items', []):
            st = it.get('statistics', {})
            if 'viewCount' not in st:          # 再生数非公開はランキングに載せられない
                continue
            why = unplayable(it)
            if why:                            # 観られない動画は載せない＝下位が自動で繰り上がる
                dropped.append(why)
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
    if dropped:
        # 握り潰さず必ず出す（最上位ルール(-0.4)「判定より先に数を出す」と同じ思想）
        print('   └ 除外 %d件: %s' % (len(dropped), ', '.join(sorted(set(dropped)))))
    return out


def cap_channel(vids, cap=3):
    """1つのタブが同じチャンネルで埋め尽くされるのを防ぐ（同チャンネルは cap 本まで）。

    2026-08-26 実測: 「怖い」20本中18本(90%)、「おもしろ」20本中17本(85%)が
    Fischer's 1チャンネルだった。再生数順としては正しいが、
    「ジャンルの棚」としては機能しないので上限を設ける。

    ⚠️ theme_videos() の中でだけ使う。
       うっちーPの歌タブ(own_songs)は100%自チャンネルで正しいので、絶対に通さないこと。
    """
    out, cnt = [], {}
    for v in vids:
        c = v.get('channelTitle', '')
        if cnt.get(c, 0) >= cap:
            continue
        cnt[c] = cnt.get(c, 0) + 1
        out.append(v)
    return out


def search_ids(q, dur, days=None):
    """1クエリ分の動画IDを返す。days を指定するとその日数以内に公開された動画に限定する。

    maxResults=50 は search.list の上限。**search.list は1回100ユニット固定で
    maxResults を増やしても消費は変わらない**ので、候補は取れるだけ取る。
    """
    p = {'part': 'id', 'type': 'video', 'order': 'viewCount', 'q': q,
         'regionCode': REGION, 'relevanceLanguage': 'ja',
         'videoDuration': dur, 'safeSearch': 'moderate', 'maxResults': 50}
    if days:
        after = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        p['publishedAfter'] = after.strftime('%Y-%m-%dT%H:%M:%SZ')
    s = api('search', p)
    return [i['id']['videoId'] for i in s.get('items', [])
            if i.get('id', {}).get('videoId')]


def theme_videos(queries, dur):
    """複数クエリをマージして重複を除き、再生数順で上位を返す。
    1語だけだと結果が偏るため、カテゴリごとに2クエリへ分けて広く拾う設計。

    🚨 期間を絞る理由（2026-08-26 実測で判明した最大の欠陥）:
      期間指定なしで order=viewCount を投げると、返るのは「歴代いちばん再生された動画」。
      実測では13タブ中9タブの公開日中央値が3年以上前（おもしろ・怖いは7.8年前）で、
      直近1年の動画はサイト全体の15%しかなかった。
      歴代ランキングは明日も同じ顔ぶれなので、**「毎日更新」が事実上ウソになる**。
      → FRESH_DAYS 以内に公開された動画の中での再生数順にして、中身が実際に入れ替わるようにする。

    フォールバック設計: 窓を絞ると候補が足りなくなるテーマがありうるので、
    LIST_N の半分も集まらなければ期間指定なしで取り直す。
    **絞りすぎて空になり、部分失敗ガードでサイトが1日まるごと更新されなくなるのを防ぐ**
    （守りすぎて壊さない）。フォールバックが起きた日は必ずログに出す。
    """
    ids = []
    for q in queries:
        for vid in search_ids(q, dur, FRESH_DAYS):
            if vid not in ids:                 # 同じ動画が2クエリに出るので重複除去
                ids.append(vid)
    vids = cap_channel(dedupe_titles(sorted(hydrate(ids), key=lambda x: -x['views'])))

    # 補完は「ほぼ空」のときだけ。20本に満たない日は、少ないまま出すほうが正しい
    # （中身が新しいことのほうが、20本ぴったり並ぶことより価値がある）。
    # search.list には総合10,000ユニットとは**別枠の日次上限**があり、
    # 全テーマで補完を走らせると検索回数が倍になって先にそこが尽きる。
    # 尽きると部分失敗ガードでサイトが1日まるごと更新されないので、補完回数自体に上限を置く。
    # 🚨 0本だけは FALLBACK_MAX を無視して必ず救う。
    #    main() は「1テーマでも空なら何も書かずに exit(1)」なので、
    #    1タブの空が **サイト全体を1日更新させない** ことに直結する。
    global _fallback_used
    if not vids or (len(vids) < 5 and _fallback_used < FALLBACK_MAX):
        _fallback_used += 1
        print('   └ 直近%d日では%d本しか集まらず、期間指定なしで補完（%d/%d回目）'
              % (FRESH_DAYS, len(vids), _fallback_used, FALLBACK_MAX))
        for q in queries:
            for vid in search_ids(q, dur):
                if vid not in ids:
                    ids.append(vid)
        vids = cap_channel(dedupe_titles(sorted(hydrate(ids), key=lambda x: -x['views'])))
    elif len(vids) < LIST_N:
        print('   └ 直近%d日の該当は%d本（20本に満たないが、新しさを優先してこのまま出す）'
              % (FRESH_DAYS, len(vids)))
    return vids[:LIST_N]


def norm_title(t):
    """比較用にタイトルを正規化する。記号・空白・大小文字の違いを潰す。"""
    return ''.join(ch for ch in t.lower() if ch.isalnum())[:24]


def dedupe_titles(vids):
    """ほぼ同じタイトルの動画を除く。
    同じ内容が連番・再アップ・自動生成で複数出るチャンネルがあり、
    そのままだとベスト3が同じ動画で埋まる（2026-08-26「びっくり」で実際に発生）。"""
    out, seen = [], set()
    for v in vids:
        k = norm_title(v['title'])
        if k and k in seen:
            continue
        seen.add(k)
        out.append(v)
    return out


def trending():
    """総合タブ=今日の急上昇。mostPopular は1ユニットで済む公式エンドポイント。

    maxResults を 50（APIの上限）にしているのは、unplayable() の除外が効いたときに
    20位まで埋まらなくなるのを防ぐため。取得件数を増やしてもクォータは1ユニットのまま。
    """
    v = api('videos', {'part': 'id', 'chart': 'mostPopular',
                       'regionCode': REGION, 'maxResults': 50})
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


def feature(p):
    """タブ先頭の「まず観る1本」枠。横長サムネ＋説明の1枚もの。
    ランキングとは別枠にしてあるので、再生数の順位を偽らずに入口動画を見せられる。"""
    return ('<a class="feat" href="https://www.youtube.com/watch?v=' + html.escape(p['videoId']) +
            '" target="_blank" rel="noopener">'
            '<span class="ftw"><img loading="lazy" src="' + html.escape(p['thumb']) + '" alt="">'
            '<i>' + html.escape(p['duration']) + '</i></span>'
            '<span class="fmt"><em>' + html.escape(p['lead']) + '</em>'
            '<b>' + html.escape(p['title']) + '</b>'
            '<span class="fnote">' + html.escape(p['note']) + '</span>'
            '<span class="fviews">▶ ' + big(p['views']) + '</span></span></a>')


def render(d):
    tabs, panes = [], []
    for i, t in enumerate(d['themes']):
        on = ' on' if i == 0 else ''
        own = ' own' if t['key'] == OWN_KEY else ''
        tabs.append('<button class="tab' + on + own + '" data-t="' + t['key'] + '">' +
                    html.escape(t['label']) + '</button>')
        pin = feature(t['pinned']) if t.get('pinned') else ''
        panes.append('<section class="pane' + on + '" id="p-' + t['key'] + '">'
                     '<div class="lead"><h2>' + html.escape(t['label']) + ' ベスト3</h2>'
                     '<p>' + html.escape(t['note']) +
                     ('<br><small>※このタブは ' + html.escape(t.get('asof', '')) +
                      ' 時点のままです（本日の取得に失敗したため）</small>' if t.get('stale') else '') +
                     '</p></div>'
                     + pin +
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


def write_sitemap():
    """lastmod を当日に更新する。毎日更新されるサイトだと検索エンジンに伝えるため、
    ここが古いままだとクロール頻度が落ちる。"""
    today = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)).strftime('%Y-%m-%d')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           '  <url>\n'
           '    <loc>' + SITE_URL + '</loc>\n'
           '    <lastmod>' + today + '</lastmod>\n'
           '    <changefreq>daily</changefreq>\n'
           '    <priority>1.0</priority>\n'
           '  </url>\n'
           '</urlset>\n')
    open(os.path.join(HERE, 'sitemap.xml'), 'w', encoding='utf-8').write(xml)


# ---------------------------------------------------------------- main

def main():
    global _fallback_used
    _fallback_used = 0
    if not API_KEY:
        print('FATAL: 環境変数 YT_API_KEY が未設定'); sys.exit(1)
    hist = json.load(open(HIST, encoding='utf-8')) if os.path.exists(HIST) else {}
    # 前日の結果。取得に失敗したテーマだけ、これを引き継いで穴を空けないために使う。
    prev_themes, prev_updated = {}, ''
    if os.path.exists(DATA):
        try:
            _pd = json.load(open(DATA, encoding='utf-8'))
            prev_themes = {t['key']: t for t in _pd.get('themes', [])}
            prev_updated = _pd.get('updated', '')
        except Exception as e:
            print('WARN 前日データを読めない（引き継ぎ無しで続行）: %s' % str(e)[:80])
    data = {'region': REGION, 'site': SITE_URL,
            # GitHub Actions は UTC で動くので、表示は日本時間に直す
            'updated': (datetime.datetime.now(datetime.timezone.utc) +
                        datetime.timedelta(hours=9)).strftime('%Y-%m-%d %H:%M'),
            'themes': []}
    newhist, failed, carried = {}, [], []
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
            print('NG %s: %s' % (label, str(e)[:160]))
            vids = None
        if not vids:
            # 🚨 前日分を引き継ぐ（2026-08-26 追加）。
            #    それまでは1テーマでも欠けると何も書かずに中断していたので、
            #    検索クォータが尽きた日は **サイトが丸ごと1日更新されなかった**。
            #    壊れたデータを書かないという原則は守りつつ、
            #    「昨日のまま」で穴を埋めれば13タブは揃う。古いことは画面とログに明記する。
            keep = prev_themes.get(key)
            if keep and keep.get('videos'):
                print('   └ %s は前日分を引き継ぎ（%d本・%s 時点）'
                      % (label, len(keep['videos']), keep.get('asof') or prev_updated or '前回'))
                carried.append(label)
                theme = dict(keep, label=label, note=note,
                             asof=keep.get('asof') or prev_updated or data['updated'], stale=True)
                data['themes'].append(theme)
                for v in theme['videos']:
                    newhist[v['videoId']] = v['views']
                continue
            print('EMPTY %s（前日分も無いので穴になる）' % label)
            failed.append(label)
            continue
        for r, v in enumerate(vids, 1):
            v['rank'] = r
            prev = hist.get(v['videoId'])           # 前回実行時との差＝前日比
            v['delta'] = (v['views'] - prev) if isinstance(prev, int) else None
            newhist[v['videoId']] = v['views']
        theme = {'key': key, 'label': label, 'note': note, 'videos': vids,
                 'asof': data['updated'], 'stale': False}
        # 固定表示の1本があれば実データを取り直して添える（失敗しても本体は落とさない）
        if key in PINNED:
            try:
                got = hydrate([PINNED[key]['videoId']])
                if got:
                    theme['pinned'] = dict(got[0], lead=PINNED[key]['lead'], note=PINNED[key]['note'])
                    print('   └ pinned: %s' % got[0]['title'][:40])
                else:
                    print('   └ NG pinned: 動画が取得できない（非公開/削除の疑い）')
            except Exception as e:
                print('   └ NG pinned: %s' % str(e)[:80])
        data['themes'].append(theme)
        print('OK %s (%d)' % (label, len(vids)))

    # 🚨 部分失敗で既存サイトを上書きしない（2026-08-26 事故: 8テーマ失敗したのに
    #    「3件以上あればOK」という緩いガードを通ってしまい、13タブが5タブに欠けた状態で
    #    index.html を上書きした）。**成功が全体の大半でなければ何も書かずに落とす**のが正解。
    expected = len(jobs)
    if carried:
        # 握り潰さない（最上位ルール(-0.4)）。閾値内でも必ず数を出す。
        print('CARRIED %d/%d テーマが前日分の引き継ぎ: %s' % (len(carried), expected, carried))
    if len(carried) > expected // 2:
        print('WARN 半数以上が引き継ぎ。検索クォータか検索語の問題を疑うこと')
    if failed or len(data['themes']) < expected - 1:
        print('FATAL: %d/%d テーマしか取得できず（失敗=%s）。既存サイトを壊さないため何も書かずに中断'
              % (len(data['themes']), expected, failed or 'なし'))
        print('       原因の定番: search.list は "Search Queries per day" という'
              '総合10,000ユニットとは別枠の日次上限を持つ。1日に何度もフルビルドすると先にここが尽きる。')
        sys.exit(1)

    json.dump(data, open(DATA, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(newhist, open(HIST, 'w', encoding='utf-8'), ensure_ascii=False)
    size = render(data)
    write_sitemap()
    print('DONE themes=%d videos=%d bytes=%d failed=%s carried=%s' % (
        len(data['themes']), sum(len(t['videos']) for t in data['themes']), size,
        failed or 'なし', carried or 'なし'))


if __name__ == '__main__':
    main()
