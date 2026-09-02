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
# 🚨 2026-08-27 内田さん指摘「今日のベスト3が公開356日前とか。これだと今日じゃない」で全面見直し。
#    実測: 18タブ中17タブのベスト3に30日超が居座り、最悪は「痛い」の1954日(5.3年)前だった。
#    真因は ①窓が90日と広い ②足りないと1年→無制限まで下がる階段 ③伸びデータが薄い日は
#    総再生数順に落ちる＝古い動画ほど有利、の3つが重なっていたこと。
#    → 上の段は「直近7日」を基準にし、足りなければ 14→30日まで**しか**広げない。
#      30日より前は下の段(殿堂入り)の担当。上の段には二度と載せない。
FRESH_DAYS = 7
TODAY_MAX_AGE = 30      # 最終防波堤。何があっても**上の段(ベスト3)**にこれより古い動画は出さない
# 🚨 2026-09-02 内田さん指摘「今日のベスト3に3〜6日前が入っている。毎日見る人には困る」。
#    実測(8/28〜9/2の6ビルド): ベスト3に **6日連続で居座った動画が13本**、通常タブのベスト3は
#    公開16〜26日前が普通だった。上限(14/30日)の内側なら何日前でも同列に扱っていたのが原因。
#    → ベスト3は「いちばん新しい層」から順に埋める。旬タブは 3日→7日→14日、通常タブは 7日→14日→30日。
#      新しい層に3本(別チャンネル)あれば古い層は見ない。
HOT_TIERS = (3, 7, 14)
NORMAL_TIERS = (7, 14, 30)
# 同じ動画がベスト3に居られる連続日数。内田さん「1〜2日で伸び続けているものは可」(2026-08-27)に合わせて2日。
# 3日目からは4位以下へ下げる（消さない）。data.json の各動画に streak を持たせて数える。
TOP3_MAX_STREAK = 2

# ── 4位以下（ランキング表）を埋める枠 ───────────────────────────────
# 内田さん指定 2026-08-27:「足らない分はお勧めみたいなやつで1ヶ月ぐらいで流行ってる動画を
# ランクアップさせて20ぐらい埋めちゃえばいい。1位から4位までしかないのはまずい。
# ただしその基準はあなたが考えて、人が見たいと思うやつを入れて」
#   → 検索は**この31日の窓で1回だけ**投げ、そこから
#       ベスト3 = 上限(旬14日/通常30日)以内だけ
#       4位以下 = 残り全部を「おすすめ順」で
#     に振り分ける。7日→14日→30日と何度も投げていた以前より**検索回数はむしろ減る**。
# 🚨 検索の日次枠は **16:00 JST 境界（PT深夜0時）**。つまり「夕方以降の手作業」と
#    「翌朝06:10の自動ビルド」は**同じ枠を共有する**。2026-08-26 の夜に作り込んだせいで
#    翌朝のビルドが枯れて carry-over に落ちた（実測で上限はおよそ50回/日）。
#    → 全テーマを **1クエリ** に統一して、1回のビルドの検索を約24回→約12回へ半減させた。
#      2クエリで母数を稼ぐより、**実測で本数が出る1語を選ぶ**ほうが効く（痛い・アニメ・感動で実証）。
FILL_DAYS = 31
# 旬タブ(エンタメ・アイドル・ニュース・音楽・お金)の4位以下は14日まで。
# 2026-09-02: エンタメの末尾に 8/2 公開の訃報動画が31日間毎日出続けていた（内田さん指摘「今日の11番がいつも同じ」）。
# 旬タブでは2週間前の話題はもう「今日の棚」に置く情報ではない。検索の窓もこの日数にする＝検索回数は増えない。
HOT_FILL_DAYS = 14
FILL_MIN_VIEWS = 2000   # これ未満は「おすすめ」として並べるには弱いので入れない
# ── 検索プールの再利用（2026-09-02 新設） ─────────────────────────────
# search.list の別枠日次上限（実測およそ50回/日・16:00 JST境界）を守るため、
# 直近 POOL_TTL_H 時間以内に同じ条件で取った動画IDの一覧は pool.json から再利用する。
# 再生数・前日比は毎回 videos.list(1u) で取り直すので鮮度は落ちない。
# ＝ 夜に手作業でビルドしても翌朝の自動ビルドが検索枠を食い潰さない。
POOL_TTL_H = 12
# 同じ日に何度も走っても API を食わないための最小間隔。GitHub の cron は数時間遅れることがあるので
# daily.yml に予備の時刻を複数置いてあり（03:17/05:43/07:29 JST）、直前のビルドがこの時間以内なら後続は何もしない。
# 6h にしてあるのは、本命03:17が定刻に走った日に07:29(4.2h後)がもう一度走って「前日比」が4時間分の
# 数字になるのを防ぐため＝**朝は必ず1回だけ**。手動実行(workflow_dispatch)は環境変数 FORCE=1 で必ず走る。
MIN_INTERVAL_H = 6
POOL = os.path.join(HERE, 'pool.json')
_last_window = ''       # 直近の theme_videos が実際に使った期間（タブの説明に出す）
_last_mode = ''         # 直近の rank_today が実際に使った並べ方（同上）       # 90日で足りないジャンルを救う2段目。歴代へ飛ぶ前にここを挟む
FALLBACK_MAX = 3        # 期間指定なしでの補完を許す最大テーマ数（検索の日次別枠を守るため）

# ── カテゴリ別「急上昇」で取れる棚は、検索を一切使わずにここから取る ────────────────
# videos.list(chart=mostPopular, videoCategoryId=N) は **1ユニット**で、
# しかも YouTube 自身が判定した「今日の急上昇」がカテゴリ別にそのまま返る。
# search.list(100ユニット + 別枠の日次上限) を置き換えるので、鮮度と節約が同時に達成できる。
# 2026-08-27 実測の公開日: ゲーム[1,1,1,1,2]日前 / ニュース[2,2,5,4,2] / エンタメ[3,5,4,1,1]。
# ⚠️ 旅行(19)は日本リージョンで 404。カテゴリが無い棚は下の検索ルートに残す。
# 値は**リスト**。1カテゴリで足りない棚は複数を束ねる。
# 🚨 2026-08-27 実測（急上昇50件中、16:9カードに載る横動画の数）:
#     エンタメ24=4本 / ペット15=6本 / スポーツ17=4本 / コメディ23=10本 / 映画アニメ1=8本
#     音楽10=30本 / ゲーム20=50本 / ニュース25=38本 / 科学技術28=32本
#   エンタメの急上昇は**大半がShorts**で、そのままだとベスト3すら埋まらない（実際4本になった）。
#   内田さん「エンタメみたいな感じで大丈夫」「Yahooトピックスを参考にするのが一番いい」に沿って、
#   エンタメ=24+23+1（エンタメ＋コメディ＋映画アニメ）で束ねて母数を確保する。
CATEGORY = {
    'news':     ['25'],            # ニュース＆政治（38本＝単独で足りる）
    'ongaku':   ['10'],            # 音楽（30本）
    'game':     ['20'],            # ゲーム（50本）
    'omoshiro': ['23', '1'],       # コメディ＋映画アニメ
    'geinou':   ['24', '23', '1'], # エンタメ＋コメディ＋映画アニメ
    'kawaii':   ['15'],            # ペット＆動物（6本・4位以下は検索で補完）
    'sugoi':    ['28', '17'],      # 科学技術＋スポーツ（神業・世界記録の受け皿・36本）
}
# ⚠️ 2026-08-27 実測で外したもの:
#   'anime': '1'  → 日本の映画＆アニメ急上昇はお笑い動画が大半で、棚と合わなかった
#   'sugoi': '17' → スポーツ急上昇の非Shortsが4本しか無く、ランキング表が埋まらなかった
#   'tabi' : '19' → 日本リージョンでは 404（カテゴリ自体が使えない）
#   いずれも専用の検索語のほうが棚に合う。カテゴリは「合う棚だけ」に使う。
# ── 旬（しゅん）で分ける二層 ─────────────────────────────────────────
# 内田さん指定 2026-08-27:「ニュース・芸能・アイドル・音楽・お金は、今日のやつじゃないと
# 情報として意味がない。おもしろ・びっくり系は毎日違う面白いのが並ぶならそれでいい」。
# ただし「本当に昨日一日だけの集計だと何とも言えない」ので、1〜2日で伸び続けているものは可。
#   → 旬タブ  : 最大14日。急上昇(=数日の勢い)が取れるならそれを最優先
#   → 通常タブ: 最大30日。日替わりで面白いものが並べばよい
HOT_KEYS = {'geinou', 'idol', 'news', 'ongaku', 'kane'}
HOT_MAX_AGE = 14

# カテゴリは棚より広い（例: 24=エンタメ には2ch系の語り動画も雑学も入る）。
# そこで、カテゴリで取った「今日の急上昇」から棚に合う語を優先して並べ替える。
# ⚠️ 絞りきらない。合致が少ない日は素のカテゴリ順に戻す＝棚を空にしないことを優先する。
# 🚨 語で絞り込むのをやめた（内田さん指定 2026-08-27）。
#    「アイドル以外は全て入れちゃっていい。Yahooに上がるようなやつだったら全部いい。
#      選定基準はバズってるやつでいい、今日話題の、みたいな。そうすれば窓口が増える」
#    実際、語で絞ったら7本しか残らずスカスカになっていた。カテゴリ急上昇は
#    そのまま「今日話題のもの」なので、**絞らずに全部入れる**のが正しい。
#    タブ間でベスト3が被る件は main() の featured で処理済み。
CATEGORY_EXCLUDE = {}

SHORTS_MAX_SEC = 70     # これ以下は縦動画(Shorts)とみなす。カードが16:9なので絵が崩れる
LONG_MIN_SEC   = 1200   # 作業用BGM等 'long' 相当の下限（20分）
_fallback_used = 0      # 1回のビルド内で使った補完回数
CHANNEL_ID = 'UCGkI3Cpu_a6yvizyqQLbKKA'          # うっちーPとエンタメの世界【大人の秘密基地】
SITE_URL = 'https://fabas-official.github.io/123tube/'

DATA = os.path.join(HERE, 'data.json')
HIST = os.path.join(HERE, 'history.json')
OWNCH = os.path.join(HERE, 'ownch_top.json')
HOF   = os.path.join(HERE, 'hof.json')
INDEX = os.path.join(HERE, 'index.html')

# 殿堂入り(歴代再生数トップ)の設定。
# 歴代ランキングは日替わりしないので毎日取り直すのは検索クォータの丸損になる。
#   → キャッシュに置き、1日 HOF_PER_RUN ジャンルずつ「いちばん古いものから」更新する。
#     12ジャンル ÷ 2 = 6日で一周する＝全ジャンルが常に6日以内の鮮度。
#     APIデータの保存は30日以内という規約もこれで満たす。
# 消費は 2ジャンル × 2クエリ × 100u = 400u/日（1日上限10,000のうち4%）。
HOF_PER_RUN = int(os.environ.get('HOF_PER_RUN', '3'))   # 初回の一括投入時だけ環境変数で増やす
HOF_MAX_AGE_DAYS = 7    # 18ジャンル ÷ 3件/日 = 6日で一周。それに合わせた鮮度の上限
HOF_FORCE_AGE = 20      # ここまで古くなったら、検索枠を惜しまず必ず1件は取り直す
SEARCH_SOFT_CAP = 60    # 1回のビルドで殿堂入りに手を出してよい検索回数の上限
_search_calls = 0       # 実際に投げた search.list の回数（クォータ判断はこれで行う）

# (キー, 表示名, [検索語...], 長さ絞り込み, 説明文)
# videoDuration で Shorts を除外している。理由=カードが16:9なので縦動画だと絵が崩れるため。
THEMES = [
    # ── 1行目: 「今日の話題」系（人・出来事。日替わりがいちばん激しい棚を前に）
    # キーは geinou のまま（履歴・殿堂入りキャッシュの引き当てを壊さないため）。
    # 表示名だけ「エンタメ」にした＝中身は YouTube のエンタメ急上昇そのもので、
    # 芸能・歌手・バラエティ・話題の人まで全部この棚に入る（内田さん指定 2026-08-27）。
    # 🚨 2026-09-02 実測: エンタメ系カテゴリ急上昇(24+23+1)の150本は **横長の動画が0本**（全部縦のショート）。
    #    旧検索語「話題 芸能 エンタメ」も14日で数本しか無く、タブが11本止まり＆31日前の訃報が毎日末尾に残っていた。
    #    「芸能界」は14日で49本(横長)・3日以内10本。芸能人ランキング・バラエティ・裏話まで広く取れる。
    ('geinou',   'エンタメ',   ['芸能界'],                                           'medium',
     '芸能・歌手・バラエティまで、いま話題になっているものをまとめて。'),
    # 実測 2026-09-02: 上の1語だけだと14日で27本(2,000回超は半分以下)。「アイドル」で47本(全部2,000回超・41ch)。
    ('idol',     'アイドル',   ['アイドル ライブ パフォーマンス', 'アイドル'], 'medium',
     'ステージも、その裏側も。推しがいる人のための棚です。'),
    ('news',     'ニュース',   ['ニュース 解説 わかりやすい'],                        'medium',
     'いま話題になっていることを、解説つきで。毎日いちばん入れ替わります。'),
    ('omoshiro', 'おもしろ',   ['爆笑 ドッキリ'],                                     'medium',
     '爆笑・ドッキリ・珍事件。何も考えずに笑いたい日に。'),
    ('bikkuri',  'びっくり',   ['衝撃映像'],                                         'medium',
     '奇跡・衝撃・珍しい瞬間。思わず二度見するやつだけ。'),
    # 実測 2026-09-02: 「猫 犬 かわいい」は31日で2,000回未満が12本混ざる。「かわいい 動物」で49本(47本が2,000回超)。
    ('kawaii',   'かわいい',   ['猫 犬 かわいい', 'かわいい 動物'],                   'medium',
     '犬・猫・赤ちゃん。無心で観たい時のための棚です。'),
    # 実測 2026-09-02: 「飯テロ グルメ 食べ歩き」は31日で18本止まり。「大食い」で50本(全部2,000回超・29ch・東海オンエア級)。
    ('meshi',    '飯うま',     ['飯テロ グルメ 食べ歩き', '大食い'],                  'medium',
     '大食い・爆食・グルメ。お腹が空く覚悟のある人だけどうぞ。'),
    ('game',     'ゲーム',     ['ゲーム実況 神プレー'],                               'medium',
     '実況・神プレー・やらかし。自分でやらなくても面白いところだけ。'),
    # 🚨 アニメは本編の無断アップが多いジャンル。公式PVと考察・解説へ寄せた検索語にして、
    #    切り抜き本編が上位に来にくいようにしている（2026-08-27 設置時の判断）。
    # 実測 2026-08-27: 旧「アニメ 公式 PV 最新」は31日で6本しか無かった。
    # 「アニメ 考察 解説」は50本取れて、しかも本編無断アップではなく考察・解説が並ぶ。
    ('anime',    'アニメ',     ['アニメ 考察 解説'],                                  'medium',
     '公式PVと、名シーンの考察・解説。本編の無断転載は載せない方針です。'),
    # ── 2行目: 「気分で選ぶ」系（感情・用途。最後は流しっぱなしのBGMで締める）
    ('ongaku',   '音楽',       ['MV 新曲 音楽'],                                      'medium',
     'MV・歌ってみた・弾いてみた。耳がよろこぶ棚です。'),
    # 実測 2026-08-27: 「感動 泣ける 実話」は16本止まり。「感動 泣ける 話」で50本。
    ('kandou',   '感動',       ['感動 泣ける 話'],                                    'medium',
     '泣きたい時は、泣いたほうがいい。'),
    # 実測 2026-09-02: 「人間関係 悩み 対処法」だけだと31日で2,000回超が11本しか無く、タブが11本止まり。
    # 「恋愛」を足すと31日で49本(全部2,000回超・27チャンネル・ABEMA/Netflix/オリコン等)。2クエリ分の消費は許容。
    ('renai',    '恋愛・人間関係', ['人間関係 悩み 対処法', '恋愛'],                  'medium',
     '恋愛と人づきあいの話。うちの本業にいちばん近い棚です。'),
    ('tabi',     '旅行',       ['旅行 vlog 絶景'],                                   'medium',
     '絶景・食べ歩き・ひとり旅。行った気になれる棚です。'),
    ('sugoi',    'すごい',     ['職人技 神業'],                                       'medium',
     '職人技・神業・世界記録。人間ってすごい。'),
    ('kowai',    '怖い',       ['心霊 怪談'],                                         'medium',
     '心霊・怪談・都市伝説。ひとりで観る勇気がある人向け。'),
    # 🚨 実測 2026-08-27: 旧「失敗 転倒 痛い」は31日で **0件**、「やらかし 失敗集」も2件で、
    #    このタブが1本しか出ない原因そのものだった。「失敗 ハプニング 爆笑」で13本取れる。
    # 実測 2026-09-02: 上の1語だけだと31日で20本→表示10本。「ハプニング 集」を足すと+47本(2,000回超25本)。
    ('itai',     '痛い',       ['失敗 ハプニング 爆笑', 'ハプニング 集'],             'medium',
     '見てるこっちが痛い、やらかしの記録。'),
    # 借金は独立させず「お金」に統合。守り(節約・借金)と攻め(投資)の2クエリで
    # 家計から資産形成までを1つの棚に収める（内田さん指示 2026-08-27）。
    # 🚨 クエリは1本に絞る（内田さん指示 2026-08-27「いろいろにするとたくさん使ってしまう」）。
    #    search.list は1回100ユニット固定なので、クエリを増やすと消費が線形に増える。
    #    語を詰め込むと逆に狭くなるので、広い3語だけを置いて母数を稼ぐ。
    #    節約・貯金・借金・株・NISA・iDeCo はこの3語のいずれかに引っかかる。
    ('kane',     'お金',       ['お金 投資 節約'],                                   'medium',
     '節約・貯金・借金の話から、株・投資・NISA・iDeCoまで。他人事じゃないお金の話。'),
    ('bgm',      '作業用BGM',  ['作業用BGM 集中'],                                  'long',
     '手を止めずに流しっぱなしでどうぞ。'),
]

# タブのボタンでだけ使う短い呼び名。7個×2段に収めるための措置で、
# 見出し・説明文では正式名称（THEMES の表示名）をそのまま使う。
SHORT_LABEL = {'bgm': 'BGM', 'renai': '恋愛'}

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


def dur_seconds(iso):
    """ISO8601(PT1H2M3S) -> 秒。読めなければ 0（=不明扱いで弾かない）。"""
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso or '')
    if not m:
        return 0
    h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + sec


def age_days(v):
    """公開からの日数。日付が読めない動画は 10**6 を返して「古い側」に寄せる
    （読めないものを新しい扱いにすると、上の段に紛れ込むのを止められないため）。"""
    try:
        y, m, d = [int(x) for x in v.get('publishedAt', '').split('-')]
        return (datetime.date.today() - datetime.date(y, m, d)).days
    except Exception:
        return 10 ** 6


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


def landscape(it):
    """横長なら True、縦(ショート)なら False、判定材料が無ければ None。

    player.embedWidth / embedHeight は videos.list に maxHeight を付けた時だけ返る公式の値。
    横長は 1080x720、縦は 405x720、正方形は 720x720（2026-09-02 実測）。正方形も
    16:9 のカードでは左右が空くので横長扱いにしない。
    """
    pl = it.get('player') or {}
    try:
        w, h = int(pl.get('embedWidth') or 0), int(pl.get('embedHeight') or 0)
    except (TypeError, ValueError):
        return None
    if not w or not h:
        return None
    return w > h


def hydrate(ids, drop_vertical=True):
    """IDリスト -> 実統計付きデータ。videos.list は50件までなので分割して呼ぶ。

    videos.list のクォータは part の数に関係なく 1ユニット固定なので、
    status や player を足しても消費は増えない。

    🚨 縦動画(ショート)の判定は **長さではなく縦横比** で行う（2026-09-02）。
       ショートは今は3分まで作れるので「70秒以下」の判定では 2〜3分の縦動画が素通りし、
       エンタメのベスト3が全部縦動画（16:9カードで上下が切れる）になっていた。
       ⚠️ 値が返らない動画は弾かない（証拠がある時だけ弾く＝守りすぎて棚を空にしない）。
       うっちーPの歌と固定枠は drop_vertical=False（自分の動画は縦でも載せる）。
    """
    out, dropped = [], []
    for i in range(0, len(ids), 50):
        v = api('videos', {'part': 'snippet,statistics,contentDetails,status,player',
                           'id': ','.join(ids[i:i + 50]), 'maxResults': 50, 'maxHeight': 720})
        for it in v.get('items', []):
            st = it.get('statistics', {})
            if 'viewCount' not in st:          # 再生数非公開はランキングに載せられない
                continue
            why = unplayable(it)
            if why:                            # 観られない動画は載せない＝下位が自動で繰り上がる
                dropped.append(why)
                continue
            land = landscape(it)
            if drop_vertical and land is False:
                dropped.append('vertical')     # 縦動画はカードに合わないので載せない
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
                # 秒数も持つ。カテゴリ別急上昇には videoDuration の絞り込みが無いので、
                # Shorts(縦動画)をここで弾くために使う。
                '_secs': dur_seconds(it.get('contentDetails', {}).get('duration', '')),
                'landscape': land,             # True=横長 / False=縦 / None=判定不能
                'thumb': (th.get('medium') or th.get('high') or th.get('default') or {}).get('url', ''),
            })
    if dropped:
        # 握り潰さず必ず出す（最上位ルール(-0.4)「判定より先に数を出す」と同じ思想）
        print('   └ 除外 %d件: %s' % (len(dropped), ', '.join(sorted(set(dropped)))))
    return out


def spread_top3(vids):
    """ベスト3が同じチャンネルで埋まらないようにする（2026-08-26 追加）。

    「ニュース」で1〜3位すべてが同じ局の同じシリーズになった。再生数順としては正しいが、
    大きく出るカードは3枚だけなので、そこが同じ配信者3連続だと「まとめ」に見えない。
    4位以下の並びは動かさず、**先頭3枠だけ**違うチャンネルになるよう繰り上げる。
    （チャンネルが3つ未満しか無いジャンルでは、できる範囲でだけ効く）
    """
    top, rest, used = [], [], set()
    for v in vids:
        c = v.get('channelTitle', '')
        if len(top) < 3 and c not in used:
            used.add(c)
            top.append(v)
        else:
            rest.append(v)
    return top + rest


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
    global _search_calls
    _search_calls += 1                             # 殿堂入りを回すかの判断に使う実消費カウンタ
    s = api('search', p)
    return [i['id']['videoId'] for i in s.get('items', [])
            if i.get('id', {}).get('videoId')]


def read_json(path, default):
    """壊れた/空のキャッシュで毎日の更新を止めない。読めなければ既定値を返す。

    🚨 2026-08-27: history.json が0バイトになっていて、その json.load でビルドが
       いきなり例外死した。**過去の記録が壊れているだけで今日の更新を落とすのは間違い**。
       読めなかったことはログに必ず出す（黙って握り潰さない）。
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print('WARN %s を読めない（%s）。空として続行する'
              % (os.path.basename(path), str(e)[:60]))
        return default


def write_json(path, obj):
    """JSONを安全に書く。一時ファイルへ書き切ってから置き換える。

    🚨 2026-08-27 事故対応: 以前は `json.dump(obj, open(path,'w'))` と書いていた。
       open('w') はその場で中身を消すので、書き終える前に落ちると **空ファイル**が残る。
       実際に data.json と history.json が0バイトになった。
       一時ファイルに書き切って os.replace で差し替えれば、途中で落ちても元が残る。
    """
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


_pool = None            # pool.json の中身（起動時に1回だけ読む）
_pool_dirty = False


def _now_jst():
    """GitHub Actions は UTC で動くので、記録・表示はすべて日本時間に揃える。"""
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)


def pool_key(key, days, queries):
    """キャッシュの鍵は「棚・窓・検索語」の組。検索語を差し替えた日に古い語の結果を再利用しないため。"""
    return '%s:%d:%s' % (key, days, '|'.join(queries or []))


def pool_get(key, days, queries=None):
    """直近 POOL_TTL_H 時間以内に同じ条件で取った検索結果(動画IDの一覧)があれば返す。無ければ None。"""
    global _pool
    if _pool is None:
        _pool = read_json(POOL, {})
    e = _pool.get(pool_key(key, days, queries))
    if not e or not e.get('ids'):
        return None
    try:
        t = datetime.datetime.strptime(e.get('fetched', ''), '%Y-%m-%d %H:%M')
        age_h = (_now_jst().replace(tzinfo=None) - t).total_seconds() / 3600.0
    except Exception:
        return None
    if age_h < 0 or age_h > POOL_TTL_H:
        return None
    print('   └ 検索プール再利用（%.1fh前に取得・検索0回）' % age_h)
    return list(e['ids'])


def pool_put(key, days, ids, queries=None):
    global _pool, _pool_dirty
    if _pool is None:
        _pool = read_json(POOL, {})
    # 同じ棚の古い鍵（前の検索語・前の窓）は消す＝ファイルが際限なく育たない
    for k in [k for k in _pool if k.split(':')[0] == key]:
        del _pool[k]
    _pool[pool_key(key, days, queries)] = {'fetched': _now_jst().strftime('%Y-%m-%d %H:%M'),
                                           'ids': list(ids)}
    _pool_dirty = True


def pool_save():
    """変更があった時だけ書く（差分の無い日に commit を汚さない）。"""
    if _pool is not None and _pool_dirty:
        write_json(POOL, _pool)


def older_than_a_day(asof, now):
    """引き継いだデータが本当に古いかを、日付ではなく**経過時間**で判定する。

    同じ日に2回走ったときや、日付をまたいで数時間しか経っていないときに
    「本日の取得に失敗しました」と全タブへ出すのは事実に反する。
    毎日更新のサイトなので、24時間を超えて初めて「古い」と呼ぶ。
    形式が読めないときは安全側（古い＝注意書きを出す）に倒す。
    """
    try:
        a = datetime.datetime.strptime(asof[:16], '%Y-%m-%d %H:%M')
        b = datetime.datetime.strptime(now[:16], '%Y-%m-%d %H:%M')
        return (b - a).total_seconds() > 24 * 3600
    except Exception:
        return True


def theme_videos(queries, dur, window=None, key=None):
    """検索でそのジャンルの候補を集める。**窓は1つ、検索は1クエリ1回だけ**。

    🚨 期間を絞る理由（2026-08-26 実測で判明した最大の欠陥）:
      期間指定なしで order=viewCount を投げると、返るのは「歴代いちばん再生された動画」。
      実測では13タブ中9タブの公開日中央値が3年以上前（おもしろ・怖いは7.8年前）で、
      直近1年の動画はサイト全体の15%しかなかった。
      歴代ランキングは明日も同じ顔ぶれなので、**「毎日更新」が事実上ウソになる**。

    🚨 階段（7→14→30と何度も投げ直す方式）を 2026-08-27 に廃止した理由:
      search.list には総合10,000ユニットとは別枠の**日次上限**があり、
      投げ直すたびにそこが減る。実際その日のうちに枯れてビルドが carry-over に落ちた。
      広い窓(FILL_DAYS=31日)で**1回だけ**取れば、狭い窓の結果はその部分集合なので、
      あとは公開日で振り分けるだけで済む＝**同じ情報が3分の1の検索回数で手に入る**。
      ベスト3の鮮度は rank_today(max_age=...) が、4位以下は topup() が受け持つ。
    """
    global _last_window
    days = window or FILL_DAYS
    _last_window = '直近%d日' % days
    ids = pool_get(key, days, queries) if key else None  # 同じ日の再実行なら検索せずIDを再利用
    if ids is None:
        ids = []
        for q in queries:
            for vid in search_ids(q, dur, days):
                if vid not in ids:             # 同じ動画が2クエリに出るので重複除去
                    ids.append(vid)
        if key:
            pool_put(key, days, ids, queries)
    # ここでは cap_channel を掛けない。プールを先に3本/chへ絞ると、
    # rank_today() が「その日の伸び」で選ぶ前に再生数順で切られてしまうため。
    vids = dedupe_titles(sorted(hydrate(ids), key=lambda x: -x['views']))
    fresh = len([v for v in vids if age_days(v) <= TODAY_MAX_AGE])
    print('   └ 直近%d日で%d本（うち%d日以内=%d本）' % (days, len(vids), TODAY_MAX_AGE, fresh))
    return vids                                # 振り分けは rank_today() と topup() が行う


_cat_key = ''           # いま処理中の棚のキー（CATEGORY_EXCLUDE の引き当てに使う）


def category_videos(cats, dur):
    """カテゴリ別の「今日の急上昇」。**検索枠を一切使わず1ユニット**で、
    YouTube 自身が今日いちばん伸びていると判定した並びがそのまま返る。

    search.list(order=viewCount) との決定的な違いは、あちらが「期間内の再生数合計」で
    並ぶため古い動画ほど有利なのに対し、こちらは最初から「今日の勢い」で並んでいること。
    上の段が「今日の棚」である以上、取れるならこちらが常に正しい（2026-08-27 内田さん指摘）。
    """
    global _last_window
    _last_window = '今日の急上昇（カテゴリ別）'
    if isinstance(cats, str):
        cats = [cats]
    ids = []
    for c in cats:
        try:
            v = api('videos', {'part': 'id', 'chart': 'mostPopular', 'videoCategoryId': c,
                               'regionCode': REGION, 'maxResults': 50})
        except Exception as e:
            # 1カテゴリが落ちても他で続ける（旅行19のようにJPで404になるものがある）
            print('   └ カテゴリ%s は取得できず: %s' % (c, str(e)[:60]))
            continue
        for i in v.get('items', []):
            if i['id'] not in ids:
                ids.append(i['id'])
    vids = hydrate(ids)
    if dur == 'long':
        vids = [x for x in vids if x.get('_secs', 0) >= LONG_MIN_SEC]
    else:
        vids = [x for x in vids if x.get('_secs', 0) > SHORTS_MAX_SEC]
    # 縦横比は hydrate() が弾く。ここは名乗りだけの保険（#short / #shorts / #shortvideo）
    vids = [x for x in vids if '#short' not in x['title'].lower()]
    vids = dedupe_titles(vids)
    ng = CATEGORY_EXCLUDE.get(_cat_key or '')
    if ng:
        kept = [v for v in vids if not any(w.lower() in v['title'].lower() for w in ng)]
        if len(kept) >= 5:
            print('   └ 他タブ担当の%d本を除外（%d→%d本）' % (len(vids) - len(kept), len(vids), len(kept)))
            vids = kept
    return vids


_last_top_age = 0       # 直近の rank_today でベスト3に使った最古の公開日数（タブの説明に出す）


def rank_today(pool, hist, hof_ids, keep_order=False, tiers=NORMAL_TIERS, block=frozenset()):
    """上の段（今日の1・2・3）の並び順を決める。

    🚨 ここが「上の段と下の段で同じ画面が出ないようにする」中核（内田さん指定 2026-08-26）。
      1. **前日からの伸び(delta)の大きい順**に並べる。総再生数順だと「昔から強い動画」が
         何日も居座って日替わりにならず、下の段(殿堂入り)と同じ顔ぶれになってしまう。
         毎日すべての候補の再生数を history.json に残しているので、その差＝1日の伸び。
         **APIの追加消費はゼロ**（すでに取得済みの数値の引き算だけ）。
      2. 殿堂入りに入っている動画は**明示的に外す**。下の段は「直近90日より前」が大半なので
         普通は被らないが、90日以内に出た歴代級の特大ヒットだけは両方に載りうるため。
      3. 伸びのデータが足りない日（初回ビルド等）は総再生数順にフォールバックする。
         並びが多少弱くなっても、空にするよりはるかにマシ。

    🚨 2026-09-02 ベスト3の選び方を「新しい層から順に」へ変更（内田さん指摘）:
      tiers=(3,7,14) なら、まず公開3日以内の候補から別チャンネルで3本埋める。足りない分だけ
      7日以内→14日以内へ広げる。上限の内側なら何日前でも同列、だった以前の方式では
      伸びの絶対量が大きい古い動画が何日も居座り、通常タブのベスト3は公開16〜26日前が普通だった。
      block には「連日居座り(streak上限)」「先に他タブへ出した動画」を渡す。ベスト3からは外すが
      4位以下には残す（消さない）。block を除くと3本に満たない時だけ block も許す＝棚を空にしない。
    """
    global _last_mode, _last_top_age
    live = [v for v in pool if v['videoId'] not in hof_ids]
    if not live:
        live = list(pool)                          # 全部が殿堂入りなら除外は諦める（空にしない）
    n_hof = len(pool) - len(live)

    max_age = tiers[-1]
    # 🚨 最終防波堤（2026-08-27）。ここまでのどの経路を通っても、
    #    上の段には max_age より古い動画を出さない。前日引き継ぎで歳を取った分もここで落ちる。
    #    ただし全部落ちて空になるくらいなら、新しい順に3本だけ残す（空はタブを殺すため）。
    fresh = [v for v in live if age_days(v) <= max_age]
    if len(fresh) < 3:
        rescued = sorted(live, key=age_days)[:3]
        if rescued:
            print('   └ %d日以内が%d本しか無いため、新しい順に%d本だけ残す（最古%d日）'
                  % (max_age, len(fresh), len(rescued), age_days(rescued[-1])))
        live = rescued
    else:
        if len(fresh) < len(live):
            print('   └ 古すぎる%d本を上の段から除外（%d日超）' % (len(live) - len(fresh), max_age))
        live = fresh

    if keep_order:
        # カテゴリ別急上昇は YouTube 側の並びがそのまま「今日の勢い」。自前で並べ替えない。
        mode = '急上昇順（YouTube判定）'
        ordered = list(live)
    else:
        for v in live:
            prev = hist.get(v['videoId'])
            v['delta'] = (v['views'] - prev) if isinstance(prev, int) else None
        buzz = [v for v in live if isinstance(v.get('delta'), int) and v['delta'] > 0]
        # 閾値を20本固定にすると、候補プールの小さいジャンルが永久に伸び順にならない。
        # 「上位3枚を伸び順で埋められるだけの本数」があれば伸び順を採用する。
        need = max(3, min(LIST_N, len(live) // 2))
        if len(buzz) >= need:
            # 🚨 前日比が無い動画（＝今日初めて候補に入った新しい動画）を落とさない（2026-09-02）。
            #    以前は伸び順の日は buzz だけを並べていたので、いちばん新しい動画が
            #    「履歴が無い」という理由で初日は必ずベスト3から漏れていた（旬タブで致命的）。
            #    伸び順の後ろに、履歴の無い動画を勢い(1日あたり再生数)順で繋ぐ。
            newcomers = sorted([v for v in live if v.get('delta') is None],
                               key=lambda x: -osusume_score(x))
            ordered = sorted(buzz, key=lambda x: -x['delta']) + newcomers
            mode = '前日からの伸び順'
        else:
            ordered = sorted(live, key=lambda x: -x['views'])
            mode = '再生回数順（前日比のデータが%d本しか無いため）' % len(buzz)
    _last_mode = mode
    ordered = cap_channel(ordered)

    # ── ベスト3: 新しい層から、別チャンネルで、居座り・他タブ既出を避けて埋める ──
    top, used_ch, picked = [], set(), set()

    def take(cands):
        for v in cands:
            if len(top) >= 3:
                return
            if v['videoId'] in picked or v['channelTitle'] in used_ch:
                continue
            top.append(v)
            picked.add(v['videoId'])
            used_ch.add(v['channelTitle'])

    for t in tiers:
        # 大きく出るカードには再生数の下限を置く。新しいだけで誰も観ていない動画を
        # 1位にしない（実測: アイドルの3日以内は24回・85回・498回だった）。
        take([v for v in ordered if age_days(v) <= t and v['videoId'] not in block
              and v.get('views', 0) >= FILL_MIN_VIEWS])
        if len(top) >= 3:
            break
    if len(top) < 3:                               # 居座り・既出・下限を除くと埋まらない → 許す
        take([v for v in ordered if v['videoId'] not in picked])
    if len(top) < 3:                               # それでも足りなければチャンネル重複も許す
        used_ch.clear()
        take([v for v in ordered if v['videoId'] not in picked])
    rest = [v for v in ordered if v['videoId'] not in picked]
    # 4位以下でも再生数が極端に少ないものは末尾へ（枠が余った時だけ出る）
    rest.sort(key=lambda v: v.get('views', 0) < FILL_MIN_VIEWS)
    out = (top + rest)[:LIST_N]
    _last_top_age = max([age_days(v) for v in top] or [0])
    print('   └ 上の段: %s / 候補%d本→%d本（殿堂入り除外%d本）／ベスト3は公開%d日以内'
          % (mode, len(pool), len(out), n_hof, _last_top_age))
    return out


def osusume_score(v):
    """4位以下に並べる順番＝「人が見たいと思うか」をPMの基準で数値にしたもの。

    1) 主軸は **勢い（1日あたりの再生数）**。総再生数で並べると古い動画ほど有利になり、
       それが今回の「5年前の動画が1位」の直接の原因だった。1日あたりに直せば、
       3日で10万回の動画が、300年かけて50万回の動画に正しく勝つ。
    2) **コメント率**を軽く加点する。再生されただけでなく人が反応した＝話題になっている証拠。
       ただし主役はあくまで勢いなので、効き目は最大1.3倍までに抑える
       （コメント欄が荒れているだけの動画を上げないため）。
    3) 同じ勢いなら**新しいほう**をわずかに上にする。毎日見にくる人にとっては、
       昨日と同じ顔ぶれが並ぶことがいちばんの離脱理由になる。
    """
    days = max(1, age_days(v))
    if days >= 10 ** 5:                       # 公開日が読めない動画は最下位へ
        return 0.0
    per_day = v['views'] / float(days)
    rate = (v.get('comments', 0) / float(v['views'])) if v.get('views') else 0.0
    engage = 1.0 + min(0.3, rate * 100)       # コメント率1%で頭打ちの+30%
    recency = 1.0 if days <= 7 else max(0.85, 1.0 - (days - 7) * 0.006)
    return per_day * engage * recency


_used_ids = set()       # このビルドで既にどこかのタブに出した動画（並びの重複を減らす用）


def demote_used(vids, keep_head=0):
    """既に他のタブへ出した動画を後ろへ回す。**消さずに順番を下げるだけ**。
    カテゴリを束ねるとエンタメとおもしろのように候補が重なる棚が出るので、
    せめて並び順が同じにならないようにする（消すと棚が痩せるので消さない）。"""
    head, tail = vids[:keep_head], vids[keep_head:]
    unused = [v for v in tail if v['videoId'] not in _used_ids]
    used = [v for v in tail if v['videoId'] in _used_ids]
    return head + unused + used


def topup(vids, pool, hof, want=LIST_N, max_age=FILL_DAYS):
    """ベスト3の鮮度はそのままに、4位以下を「おすすめ順」で20位まで埋める。

    pool は検索の窓で取った候補一式。すでに載っているものと殿堂入りを除き、
    再生数が極端に少ないものも外したうえで osusume_score の高い順に足す。
    max_age より古いものは足さない（旬タブは14日・通常タブは31日。2026-09-02:
    エンタメの末尾に31日前の訃報が毎日残っていた対策）。
    cap_channel は **足したあとの全体**に掛ける。ベスト3が先頭にあるので、
    先に確定した上位は必ず残り、同じチャンネルばかりが下に並ぶことだけを防げる。"""
    if len(vids) >= want:
        return vids
    have = set(v['videoId'] for v in vids)
    cand = [v for v in pool
            if v['videoId'] not in have and v['videoId'] not in hof
            and v.get('views', 0) >= FILL_MIN_VIEWS and age_days(v) <= max_age]
    if not cand:
        return vids
    cand.sort(key=lambda x: -osusume_score(x))
    merged = cap_channel(vids + cand)[:want]
    print('   └ 4位以下を直近%d日のおすすめで補完: %d本 → %d本'
          % (max_age, len(vids), len(merged)))
    return merged


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
    return hydrate([i['id'] for i in v.get('items', [])])   # 20本への絞り込みは main() 側


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
    vids = hydrate(ids, drop_vertical=False)   # 自分の動画は縦でも載せる
    vids.sort(key=lambda x: -x['views'])
    songs = [v for v in vids if any(h in v['title'] for h in SONG_HINT)][:60]
    write_json(OWNCH, {'channelId': CHANNEL_ID, 'scanned': len(vids),
                       'updated': datetime.date.today().isoformat(), 'songs': songs})
    print('own channel rescan: %d本 -> 歌%d本' % (len(vids), len(songs)))
    return songs


def own_songs():
    """うっちーPの歌トップ。曲の顔ぶれは滅多に変わらないのでキャッシュを使い、
    再生数だけ取り直す（2ユニット）。30日を超えたらフル再走査＝APIデータ30日ルールも満たす。"""
    songs, stale = [], True
    c = read_json(OWNCH, None)
    if c:
        songs = c.get('songs', [])
        try:
            age = (datetime.date.today() -
                   datetime.date(*[int(x) for x in c.get('updated', '2000-01-01').split('-')])).days
            stale = age > 25                   # 30日ルールに余裕を持たせて25日で再走査
        except Exception:
            stale = True
    if stale or not songs:
        songs = scan_own_channel()
    fresh = hydrate([v['videoId'] for v in songs[:LIST_N]], drop_vertical=False)
    fresh.sort(key=lambda x: -x['views'])
    return fresh[:LIST_N]


# ---------------------------------------------------------------- 殿堂入り

def hof_videos(queries, dur):
    """そのジャンルの「歴代」再生数トップを返す。

    theme_videos() との違いは publishedAfter を付けないことだけ。
    付けない = order=viewCount がそのまま歴代ランキングになる。
    毎日のタブは直近90日に絞ってあるので、ここが「古いけど強い動画」の受け皿になる。
    """
    ids = []
    for q in queries:
        for vid in search_ids(q, dur):          # 期間指定なし＝歴代
            if vid not in ids:
                ids.append(vid)
    vids = spread_top3(cap_channel(dedupe_titles(sorted(hydrate(ids), key=lambda x: -x['views']))))
    return vids[:LIST_N]


def load_hof():
    """殿堂入りキャッシュを読む。壊れていても落とさず空で返す（毎日の更新を止めない）。"""
    return read_json(HOF, {}).get('themes', {}) or {}


def hof_age(entry):
    """キャッシュ1件の古さ(日)。日付が壊れていたら「とても古い」扱いにして先に更新させる。

    🚨 2026-08-27: 以前は強制更新したいエントリの updated に '2000-01-01' を書いていたが、
       その値が画面の「集計時点」にそのまま出て『26年前のランキング』に見えていた。
       目印は表示に使わない 'force' キーで持ち、updated には本当の取得日だけを入れる。
    """
    if entry.get('force'):
        return 9999
    try:
        y, m, d = [int(x) for x in entry.get('updated', '2000-01-01').split('-')]
        return (datetime.date.today() - datetime.date(y, m, d)).days
    except Exception:
        return 9999


def refresh_hof(cache, allow):
    """古い順に allow ジャンルだけ殿堂入りを取り直す。

    🚨 1件でも失敗したら **その1件だけ諦めて古いキャッシュを残す**。
       殿堂入りは「あれば嬉しい」情報で、これが原因で毎日の更新を止めてはいけない。
    """
    if allow <= 0:
        print('殿堂入り: 今日は更新しない（検索クォータ温存）')
        return cache, []
    todo = sorted(THEMES, key=lambda t: -hof_age(cache.get(t[0], {})))
    done = []
    for key, label, queries, dur, _note in todo:
        if len(done) >= allow:
            break
        age = hof_age(cache.get(key, {}))
        if age < HOF_MAX_AGE_DAYS and cache.get(key, {}).get('videos'):
            break                                # 以降はもっと新しい＝更新不要
        try:
            vids = hof_videos(queries, dur)
        except Exception as e:
            print('   └ NG 殿堂入り %s: %s（前のキャッシュを残す）' % (label, str(e)[:120]))
            continue
        if not vids:
            print('   └ NG 殿堂入り %s: 0本（前のキャッシュを残す）' % label)
            continue
        for r, v in enumerate(vids, 1):
            v['rank'] = r
            v.pop('delta', None)                 # 歴代に前日比は意味がないので持たない
        cache[key] = {'updated': datetime.date.today().isoformat(), 'videos': vids}  # force は落とす
        done.append(label)
        print('   └ 殿堂入り更新 %s (%d本・前回から%s日)' % (label, len(vids),
              '初回' if age >= 9999 else age))
    return cache, done


def save_hof(cache):
    write_json(HOF, {'updated': datetime.date.today().isoformat(), 'themes': cache})


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
    d = v.get('delta')
    if isinstance(d, int) and d > 0:
        s += ' 前日から+' + format(d, ',') + '回。'
    elif isinstance(d, int) and d < 0:
        s += ' 前日から' + format(d, ',') + '回。'
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


def delta_html(v):
    """前日比の表示。増・減・変化なし・データ無しを取り違えないようにする。

    🚨 以前は `if v.get('delta')` の1本で、マイナスの日に「+-1,234」と出たうえ
       上昇の緑が当たっていた。0（伸びていない）と None（測れていない）も同じ「—」だった。
    """
    d = v.get('delta')
    if not isinstance(d, int):
        return '<span class="flat">—</span>'
    if d > 0:
        return '<span class="up">+' + format(d, ',') + '</span>'
    if d < 0:
        return '<span class="down">−' + format(abs(d), ',') + '</span>'
    return '<span class="flat">±0</span>'


def row(v):
    delta = delta_html(v)
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


def hrow(v, rank):
    """殿堂入り用の1行。歴代ランキングに前日比は意味がないので、代わりに公開日を出す。"""
    return ('<a class="row hof" href="https://www.youtube.com/watch?v=' + html.escape(v['videoId']) +
            '" target="_blank" rel="noopener">'
            '<span class="n">' + str(rank) + '</span>'
            '<span class="rtw"><img loading="lazy" src="' + html.escape(v['thumb']) + '" alt="">'
            '<i>' + html.escape(v['duration']) + '</i></span>'
            '<span class="ri"><b>' + html.escape(v['title']) + '</b>'
            '<em>' + html.escape(v['channelTitle']) + '</em></span>'
            '<span class="rs"><u>' + format(v['views'], ',') + '</u>'
            '<i>公開 ' + html.escape(v.get('publishedAt', '')) + '</i>'
            '<i>コメント ' + format(v.get('comments', 0), ',') + '</i></span></a>')


def tab_btn(key, label, group):
    return ('<button class="tab" data-g="' + group + '" data-t="' + key + '">' +
            html.escape(label) + '</button>')


def pane(pid, on, inner):
    return '<section class="pane' + (' on' if on else '') + '" id="' + pid + '">' + inner + '</section>'


def today_pane(t, on):
    """上の段の中身＝そのジャンルの「今日のベスト3」。

    🚨 ここは総再生数の“ベスト”ではなく **その日いちばん伸びたもの** を並べている。
       総再生数順にすると昔から強い動画が何日も居座り、下の段(殿堂入り)と
       同じ顔ぶれになって段を分けた意味が消えるため（内田さん指摘 2026-08-26）。
    """
    vids = t['videos']
    stale = ('<br><small>※ ' + html.escape(t.get('asof', '')) +
             ' 時点のままです（本日の取得に失敗したため）</small>') if t.get('stale') else ''
    pin = feature(t['pinned']) if t.get('pinned') else ''
    how = ('<br><small>' + html.escape(t['how']) + '</small>') if t.get('how') else ''
    rest = ''
    if len(vids) > 3:
        rest = ('<h3 class="rh">今日の 4位〜' + str(len(vids)) + '位</h3>'
                '<div class="rows">' + ''.join(row(v) for v in vids[3:]) + '</div>')
    head = ('<div class="lead"><span class="lbadge">今日</span>'
            '<h2>' + html.escape(t['label']) + ' ベスト3</h2>'
            '<p>' + html.escape(t['note']) + how + stale + '</p></div>')
    return pane('p-today-' + t['key'], on,
                head + pin + '<div class="top3">' + ''.join(card(v) for v in vids[:3]) + '</div>' + rest)


def asof_label(entry):
    """「集計時点」に出してよい日付だけを返す。おかしな値は日付を出さない。

    未来日・パース不能・極端に古い値をそのまま出すと、実データが新しいのに
    『何年も前のランキング』に見えてしまう（2026-08-27 実際に 2000-01-01 が出た）。
    """
    u = (entry or {}).get('updated', '')
    try:
        d = datetime.date(*[int(x) for x in u.split('-')])
    except Exception:
        return '取得中'
    today = datetime.date.today()
    if d > today or (today - d).days > 60:
        return '取得中'
    return u


def hof_pane(key, label, entry, on):
    """下の段の中身＝そのジャンルの殿堂入り（歴代の再生回数トップ）。

    見出しに本数を「20」と決め打ちしない。除外が効いて19本になる日があるため、
    実際の本数をそのまま書く（内田さん指摘 2026-08-26）。
    """
    # キャッシュは取得当時の並びなので、表示のたびにベスト3のチャンネル重複だけ直す。
    # （spread_top3 を入れる前に取った分にも効かせるため。4位以下の順位は動かさない）
    vids = spread_top3(list((entry or {}).get('videos') or []))
    for i, v in enumerate(vids, 1):
        v['rank'] = i
    n = len(vids)
    head = ('<div class="lead"><span class="lbadge gold">殿堂入り</span>'
            '<h2>' + html.escape(label) + ' 歴代ベスト' + (str(n) if n else '') + '</h2>'
            '<p>公開日を問わない、これまででいちばん再生された動画です。ここは毎日は変わりません。</p></div>')
    if not vids:
        return pane('p-hof-' + key, on, head +
                    '<p class="hnote">この棚はまだ集計中です。1日' + str(HOF_PER_RUN) + 'ジャンルずつ順番に集めているので、'
                    '数日以内にここへ入ります。</p>')
    body = '<div class="top3">' + ''.join(card(v) for v in vids[:3]) + '</div>'
    if n > 3:
        body += ('<h3 class="rh gold">4位〜' + str(n) + '位</h3><div class="rows">' +
                 ''.join(hrow(v, i) for i, v in enumerate(vids[3:], 4)) + '</div>')
    body += '<p class="hnote">集計時点: ' + html.escape(asof_label(entry)) + '</p>'
    return pane('p-hof-' + key, on, head + body)


def own_pane(t, on):
    """左右のうち右側の大ボタンの中身。歌は順位がほとんど動かないので独立させている。"""
    if not t:
        return ''
    vids = t['videos']
    pin = feature(t['pinned']) if t.get('pinned') else ''
    rows = ('<h3 class="rh own">4位〜' + str(len(vids)) + '位</h3><div class="rows">' +
            ''.join(row(v) for v in vids[3:]) + '</div>') if len(vids) > 3 else ''
    head = ('<div class="lead"><span class="lbadge own">うっちーPの歌</span>'
            '<h2>再生回数トップ' + str(len(vids)) + '</h2>'
            '<p>' + html.escape(t['note']) + '</p></div>')
    return pane('p-own-' + t['key'], on,
                head + pin + '<div class="top3">' + ''.join(card(v) for v in vids[:3]) + '</div>' + rows)


def render(d, hofc):
    """内田さんの手描きイメージ通りのナビを組む（2026-08-26）。

        [ 総合 ]  | 今日のベスト3 | 飯うま かわいい … |  [ うっちーPの歌 ]
                  | 殿堂入り      | 飯うま かわいい … |

    ・総合とうっちーPの歌は左右に同じ幅の大ボタンで置き、2段のどちらにも属さない
      （総合＝当日の急上昇しか存在しない／歌＝順位がほとんど動かない、で性質が違う）
    ・中央の2段はそれぞれ枠で囲って区切る。同じジャンル名が上下に並ぶので、
      どちらの段を押したかで出るものが変わるのが直感的に分かる
    ・中身は1面だけ出す。上下を同時に開くと縦に長くなりすぎるため
    """
    themes = {t['key']: t for t in d['themes']}
    # ジャンル名を変えた日でも、台帳に残る古いラベルではなく今の定義を使う
    for _k, _l, _q2, _d2, _n2 in THEMES:
        if _k in themes:
            themes[_k] = dict(themes[_k], label=_l, note=_n2)
    genres = [(k, l) for (k, l, _q, _dur, _n) in THEMES if k in themes]

    mega_t = ('<button class="tab mega mtrend on" data-g="today" data-t="trend">'
              '<span class="mtag">今日</span><b>総合</b>'
              '<i>いま日本で伸びてる<br>ベスト20</i>'
              '<span class="mcta">見る →</span></button>') if 'trend' in themes else ''
    mega_o = ('<button class="tab mega mown" data-g="own" data-t="' + OWN_KEY + '">'
              '<span class="mtag">オリジナル</span><b>' + html.escape(OWN_LABEL) + '</b>'
              '<i>作詞はぜんぶ本人</i>'
              '<span class="mcta">聴く →</span></button>') if OWN_KEY in themes else ''

    # 列数はジャンル数の半分（切り上げ）。こうすると何ジャンルに増減しても
    # 広い画面では必ず「2段ちょうど」に収まる（内田さん指定 2026-08-26）。
    cols = max(1, -(-len(genres) // 2))
    def btns(group):
        return ('<div class="tabs" style="--cols:' + str(cols) + '">' +
                ''.join(tab_btn(k, SHORT_LABEL.get(k, l), group) for k, l in genres) + '</div>')
    r_today = ('<div class="tabrow today">'
               '<span class="rowlabel"><b>今日のベスト3</b><i>毎日入れ替わる</i></span>'
               + btns('today') + '</div>')
    r_hof = ('<div class="tabrow hof">'
             '<span class="rowlabel"><b>殿堂入りベスト' + str(LIST_N) + '</b>'
             '<i>歴代ぜんぶから</i></span>'
             + btns('hof') + '</div>')

    nav = ('<div class="navwrap">' + mega_t +
           '<div class="rowsbox">' + r_today + r_hof + '</div>' + mega_o + '</div>')

    panes = []
    if 'trend' in themes:
        panes.append(today_pane(themes['trend'], True))
    for k, _l in genres:
        panes.append(today_pane(themes[k], False))
    for k, l in genres:
        panes.append(hof_pane(k, l, hofc.get(k), False))
    panes.append(own_pane(themes.get(OWN_KEY), False))

    body = nav + '<div class="panes">' + ''.join(panes) + '</div>'
    tpl = open(os.path.join(HERE, 'template.html'), encoding='utf-8').read()
    # フッターの説明文は定数から埋める。文言と実装が食い違うと「表示の嘘」になる
    # （2026-09-02 まで「直近90日」「1日2ジャンル」と、とっくに変わった数字が残っていた）。
    hof_cycle = -(-len(THEMES) // max(1, HOF_PER_RUN))
    out = (tpl.replace('__BODY__', body).replace('__UPD__', d['updated'])
              .replace('__T_HOT__', str(HOT_TIERS[0])).replace('__C_HOT__', str(HOT_TIERS[-1]))
              .replace('__T_NORM__', str(NORMAL_TIERS[0])).replace('__C_NORM__', str(NORMAL_TIERS[-1]))
              .replace('__STREAK__', str(TOP3_MAX_STREAK)).replace('__HOF_PER_RUN__', str(HOF_PER_RUN))
              .replace('__HOF_CYCLE__', str(hof_cycle)).replace('__HOT_FILL__', str(HOT_FILL_DAYS))
              .replace('__FILL__', str(FILL_DAYS)))
    with open(INDEX + '.tmp', 'w', encoding='utf-8') as f:
        f.write(out)
        f.flush()
        os.fsync(f.fileno())
    os.replace(INDEX + '.tmp', INDEX)
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
    _sm = os.path.join(HERE, 'sitemap.xml')
    with open(_sm + '.tmp', 'w', encoding='utf-8') as f:
        f.write(xml)
    os.replace(_sm + '.tmp', _sm)


# ---------------------------------------------------------------- main

def main():
    global _fallback_used
    _fallback_used = 0
    if not API_KEY:
        print('FATAL: 環境変数 YT_API_KEY が未設定'); sys.exit(1)
    hist = read_json(HIST, {})
    # 前日の結果。取得に失敗したテーマだけ、これを引き継いで穴を空けないために使う。
    _pd = read_json(DATA, {})
    prev_themes = dict((t['key'], t) for t in _pd.get('themes', []) if t.get('key'))
    prev_updated = _pd.get('updated', '')
    now_jst = _now_jst().strftime('%Y-%m-%d %H:%M')
    # 🚨 同じ日に何度も走らない（2026-09-02）。daily.yml に予備の cron を複数置いたので、
    #    直前のビルドが MIN_INTERVAL_H 以内なら何もしない。手動実行(FORCE=1)は例外。
    if prev_updated and not os.environ.get('FORCE'):
        try:
            _gap = (datetime.datetime.strptime(now_jst, '%Y-%m-%d %H:%M') -
                    datetime.datetime.strptime(prev_updated[:16], '%Y-%m-%d %H:%M')).total_seconds() / 3600.0
        except Exception:
            _gap = 10 ** 6
        if 0 <= _gap < MIN_INTERVAL_H:
            print('SKIP 直前のビルド(%s)から%.1fh。%dh未満なので今回は何もしない（予備cronの空振り）'
                  % (prev_updated, _gap, MIN_INTERVAL_H))
            return
    data = {'region': REGION, 'site': SITE_URL,
            # GitHub Actions は UTC で動くので、表示は日本時間に直す
            'updated': now_jst,
            'themes': []}
    newhist, failed, carried, pending = {}, [], [], []
    # 🚨 連日居座りの記録（2026-09-02）。前回ベスト3だった動画の連続日数を引き継ぐ。
    #    streak を持たない古い台帳は「1日目」とみなす。
    #    大きく出るカードは各タブ3枚だけ。同じ動画が複数タブのベスト3に並ぶと「まとめ」に
    #    見えないので、先に他タブへ出した動画(_used_ids)も rank_today の block に渡す。
    prev_streak = {}
    for _k, _t in prev_themes.items():
        for _v in (_t.get('videos') or [])[:3]:
            prev_streak[(_k, _v['videoId'])] = int(_v.get('streak') or 1)
    # 連続「日数」であって連続「ビルド回数」ではない。同じ日に2回走っても+1しない
    # （2026-09-02 夜に2回ビルドしたら1日で streak=2 になり、翌朝に全部入れ替わりかけた）。
    same_day = bool(prev_updated) and prev_updated[:10] == now_jst[:10]
    streak_step = 0 if same_day else 1
    # 殿堂入りキャッシュ。上の段から歴代組を外すために**取り直す前**の中身を使う。
    # （歴代はほぼ動かないので1日古くても実害が無く、循環参照を避けられる）
    hofc = load_hof()
    hof_ids = dict((k, set(v['videoId'] for v in (e.get('videos') or [])))
                   for k, e in hofc.items())
    jobs = [('trend', '総合', None, None, '今日いちばん伸びている動画。毎日入れ替わります。')] + \
           list(THEMES) + [(OWN_KEY, OWN_LABEL, None, None, OWN_NOTE)]
    for key, label, q, dur, note in jobs:
        pool, how = [], ''
        try:
            if key == 'trend':
                # 公式の急上昇チャートそのもの＝すでに「その日いちばん伸びているもの」の並び。
                # ここだけは自前で並べ替えない（YouTube側の順位が最も正確なため）。
                pool = trending()
                vids = pool[:LIST_N]
            elif key == OWN_KEY:
                pool = own_songs()
                vids = pool
            else:
                hot = key in HOT_KEYS                     # 旬タブ=情報の鮮度が命
                tiers = HOT_TIERS if hot else NORMAL_TIERS
                fill_max = HOT_FILL_DAYS if hot else FILL_DAYS
                cat = CATEGORY.get(key)
                hofset = hof_ids.get(key, set())
                # ベスト3から外すもの: 連日居座り(streak上限) と 先に他タブへ出した動画。
                # どちらも4位以下には残す（消さない）。
                block = set(vid for (k2, vid), s in prev_streak.items()
                            if k2 == key and s >= TOP3_MAX_STREAK) | set(_used_ids)
                fill = []                                # 4位以下を埋めるための候補
                if cat:
                    # カテゴリがある棚は検索を使わず1ユニット。急上昇の並びをそのまま使う
                    globals()['_cat_key'] = key
                    pool = category_videos(cat, dur)
                    vids = rank_today(pool, hist, hofset, keep_order=True, tiers=tiers, block=block)
                    fresh3 = [v for v in vids[:3] if age_days(v) <= tiers[-1]]
                    # 急上昇の横長動画は日によって数本しか無い（実測: かわいい6本・エンタメ0本）。
                    # ベスト3は急上昇を優先し、足りない分と4位以下を検索で埋める。
                    # 検索が尽きていても**急上昇の分は必ず出る**ので、失敗しても続行する。
                    if (len(vids) < LIST_N or len(fresh3) < 3) and q:
                        try:
                            fill = theme_videos(q, dur, window=fill_max, key=key)
                        except Exception as e:
                            print('   └ 検索での補完は見送り（%s）' % str(e)[:60])
                    if len(fresh3) < 3 and fill:
                        # カテゴリだけではベスト3が埋まらない → 急上昇を先頭に、検索候補をおすすめ順で
                        # 後ろに繋いで選び直す（急上昇の並びは崩さない）
                        seen = set(v['videoId'] for v in pool)
                        extra = sorted([v for v in fill if v['videoId'] not in seen],
                                       key=lambda x: -osusume_score(x))
                        print('   └ 急上昇だけでは%d日以内が%d本。検索候補%d本を混ぜて選び直す'
                              % (tiers[-1], len(fresh3), len(extra)))
                        pool = pool + extra
                        vids = rank_today(pool, hist, hofset, keep_order=True, tiers=tiers, block=block)
                        globals()['_last_window'] = '今日の急上昇＋直近%d日の検索' % fill_max
                    else:
                        globals()['_last_window'] = '今日の急上昇（カテゴリ別）'
                else:
                    # カテゴリが無い棚は検索。**1つの窓で1回だけ**投げ（旬14日／通常31日）、
                    # ベスト3は新しい層から埋め、残りをそのまま4位以下に回す。
                    pool = theme_videos(q, dur, window=fill_max, key=key)
                    vids = rank_today(pool, hist, hofset, tiers=tiers, block=block)
                    fill = pool
                if fill:
                    vids = topup(vids, fill, hofset, max_age=fill_max)
                # 4位以下に再生数が極端に少ない動画（数百回）を並べない。10本残るなら切る。
                # 「20本埋める」より「人が見たいと思うものだけ」が内田さんの基準（2026-08-27）。
                _ok = [v for v in vids if v.get('views', 0) >= FILL_MIN_VIEWS]
                if len(_ok) >= 10 and len(_ok) < len(vids):
                    print('   └ 再生%d回未満の%d本を外す（%d本→%d本）'
                          % (FILL_MIN_VIEWS, len(vids) - len(_ok), len(vids), len(_ok)))
                    vids = _ok
                vids = demote_used(vids, keep_head=3)    # ベスト3は確定済み。4位以下だけ既出を後ろへ
                for v in vids:
                    _used_ids.add(v['videoId'])
                how = '%s／%s／ベスト3は公開%d日以内' % (_last_window, _last_mode, _last_top_age)
        except Exception as e:                      # 1テーマ失敗で全体を落とさない
            print('NG %s: %s' % (label, str(e)[:160]))
            vids = None
        if vids and key not in ('trend', OWN_KEY):
            # 連続日数を記録する。ベスト3に入った動画は前回の値+1、外れた動画は消す。
            for v in vids:
                v.pop('streak', None)
            for v in vids[:3]:
                _p = prev_streak.get((key, v['videoId']))
                v['streak'] = (_p + streak_step) if _p else 1
            _long = [v for v in vids[:3] if v['streak'] > TOP3_MAX_STREAK]
            if _long:
                print('   └ ⚠️ 代わりが無く%d日目のベスト3を許容: %s'
                      % (TOP3_MAX_STREAK + 1, ', '.join(v['title'][:20] for v in _long)))
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
                # 「今日すでに取れていた分」を引き継いだだけなら古くない。
                # 同じ日のうちに2回走った時に全タブへ「古いです」と出すのは事実に反する。
                _asof = keep.get('asof') or prev_updated or data['updated']
                theme = dict(keep, label=label, note=note, asof=_asof,
                             stale=older_than_a_day(_asof, data['updated']),
                             carried_days=int(keep.get('carried_days', 0)) + 1)
                data['themes'].append(theme)
                for v in theme['videos']:
                    newhist[v['videoId']] = v['views']
                continue
            # 新設したばかりのジャンルは前日分が無くて当たり前。
            # それを「失敗」に数えると、1つ足しただけでサイト全体が更新されなくなる。
            # 取れるようになるまで、そのタブは出さずに待てばいい。
            if key not in prev_themes:
                print('SKIP %s（新設・今回は取得できず。次回に持ち越し）' % label)
                pending.append(label)
            else:
                print('EMPTY %s（前日分も無いので穴になる）' % label)
                failed.append(label)
            continue
        # 🚨 履歴は候補プール全部を残す（表示した20本だけだと、翌日ランク外から
        #    上がってきた動画の「伸び」が計算できず、上の段が日替わりにならない）。
        for v in pool:
            newhist[v['videoId']] = v['views']
        for r, v in enumerate(vids, 1):
            v['rank'] = r
            if v.get('delta') is None:              # rank_today が入れていればそのまま使う
                prev = hist.get(v['videoId'])
                v['delta'] = (v['views'] - prev) if isinstance(prev, int) else None
            newhist[v['videoId']] = v['views']
        theme = {'key': key, 'label': label, 'note': note, 'videos': vids,
                 'asof': data['updated'], 'stale': False, 'how': how, 'carried_days': 0}
        # 固定表示の1本があれば実データを取り直して添える（失敗しても本体は落とさない）
        if key in PINNED:
            try:
                got = hydrate([PINNED[key]['videoId']], drop_vertical=False)
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
    expected = len(jobs) - len(pending)      # 新設で未取得のものは母数から外す
    if pending:
        # 握り潰さない（最上位ルール(-0.4)）。出せなかったタブは必ず名前を出す。
        print('PENDING %d ジャンルが新設・未取得（タブはまだ出ない）: %s' % (len(pending), pending))
    if carried:
        # 握り潰さない（最上位ルール(-0.4)）。閾値内でも必ず数を出す。
        print('CARRIED %d/%d テーマが前日分の引き継ぎ: %s' % (len(carried), expected, carried))
    # 何日連続で引き継いでいるかを必ず出す。日数を出さないと、同じ滞りが何日続いても
    # 「既知issue」として見えなくなる（CLAUDE.md 最上位ルール(-0.4)）。
    _stuck = [(t['label'], t.get('carried_days', 0)) for t in data['themes']
              if t.get('carried_days', 0) >= 1]
    if _stuck:
        print('STALL 引き継ぎ日数: %s' % ', '.join('%s=%d日' % x for x in sorted(_stuck, key=lambda y: -y[1])))
    _worst = max([d for _l, d in _stuck] or [0])
    if _worst >= 3:
        print('ESCALATE %d日以上更新できていないジャンルがある。検索語かクォータを見直すこと' % _worst)
    if len(carried) > expected // 2:
        print('WARN 半数以上が引き継ぎ。検索クォータか検索語の問題を疑うこと')
    # 🚨 以前は `if failed or ...` だったので、1ジャンルが空になっただけで
    #    17/18 取れていてもサイトが更新されなかった（緩和条件が死んでいた）。
    #    穴が1つなら、そのタブを出さずに残りを更新するほうが読者の利益になる。
    if failed:
        print('WARN %d ジャンルが空（そのタブは出さない）: %s' % (len(failed), failed))
    if len(data['themes']) < expected - 1:
        print('FATAL: %d/%d テーマしか取得できず（失敗=%s）。既存サイトを壊さないため何も書かずに中断'
              % (len(data['themes']), expected, failed or 'なし'))
        print('       原因の定番: search.list は "Search Queries per day" という'
              '総合10,000ユニットとは別枠の日次上限を持つ。1日に何度もフルビルドすると先にここが尽きる。')
        sys.exit(1)

    # 殿堂入りは毎日のタブが全部揃ってから、余った枠で少しずつ取り直す。
    # 補完(fallback)を使った日は検索の日次別枠が危ないのでスキップする＝毎日の更新を最優先。
    # 🚨 以前は「補完を1回でも使った日は殿堂入りを止める」だったが、_fallback_used は
    #    全ジャンル共有のカウンタなので、18分の1でも薄いジャンルがあれば毎日全停止し、
    #    殿堂入りが永久に更新されなくなる（＝APIデータ30日ルールにも抵触する）。
    #    実際に投げた検索回数で判断し、さらに極端に古いものは枠を無視して必ず1件救う。
    if _search_calls < SEARCH_SOFT_CAP:
        allow = HOF_PER_RUN
    elif any(hof_age(e) >= HOF_FORCE_AGE for e in hofc.values()):
        allow = 1                                   # 古すぎるものだけは何としても1件更新する
    else:
        allow = 0
    print('殿堂入り判定: search %d回 / 上限%d → 今回%d件' % (_search_calls, SEARCH_SOFT_CAP, allow))
    hofc, hof_done = refresh_hof(hofc, allow)
    save_hof(hofc)
    _ages = sorted(hof_age(e) for e in hofc.values())
    print('殿堂入り: %d/%dジャンル保有・最古%s日・今日更新=%s'
          % (len(hofc), len(THEMES), _ages[-1] if _ages else '—', hof_done or 'なし'))

    write_json(DATA, data)
    write_json(HIST, newhist)
    pool_save()                                     # 検索プールを残す＝同じ日の再実行で検索枠を使わない
    size = render(data, hofc)
    write_sitemap()
    print('DONE themes=%d videos=%d bytes=%d failed=%s carried=%s' % (
        len(data['themes']), sum(len(t['videos']) for t in data['themes']), size,
        failed or 'なし', carried or 'なし'))


def render_only():
    """APIを1回も叩かずに index.html だけ作り直す（見た目の調整用・消費0ユニット）。

    テンプレートをいじるたびにフルビルドしていると検索クォータが無駄に減るので、
    描画だけやり直せる口を用意しておく。
    """
    # 台帳(data.json)は main() が並べ終えた最終形なので、ここでは並べ替えずそのまま描く。
    # （以前はここで rank_today を掛け直していたが、旬タブの層や居座り判定を持たないまま
    #   並べ直すので本番と違う画面になっていた。描画確認用の口が本番と違ってはいけない）
    data = read_json(DATA, {'updated': '', 'themes': []})
    hofc = load_hof()
    size = render(data, hofc)
    print('RENDER-ONLY bytes=%d themes=%d' % (size, len(data.get('themes', []))))


def fill_missing():
    """THEMES にあって data.json に無いジャンルだけを取りに行く。

    ジャンルを1つ足すたびにフルビルドすると検索を22回も無駄打ちするので、
    不足分だけ取って既存データにマージする口を用意しておく（1ジャンル200ユニット）。
    """
    if not API_KEY:
        print('FATAL: 環境変数 YT_API_KEY が未設定'); sys.exit(1)
    data = read_json(DATA, None)
    if not data or not data.get('themes'):
        print('FATAL: data.json が読めない/空。--fill-missing は既存台帳への追記専用'); sys.exit(1)
    have = set(t['key'] for t in data.get('themes', []))
    hist = read_json(HIST, {})
    todo = [t for t in THEMES if t[0] not in have]
    if not todo:
        print('不足ジャンルなし'); return
    print('不足 %d ジャンル: %s' % (len(todo), [t[1] for t in todo]))
    added = []
    for key, label, queries, dur, note in todo:
        try:
            hot = key in HOT_KEYS
            pool = theme_videos(queries, dur, window=HOT_FILL_DAYS if hot else FILL_DAYS, key=key)
            vids = rank_today(pool, hist, set(), tiers=HOT_TIERS if hot else NORMAL_TIERS)
        except Exception as e:
            print('NG %s: %s' % (label, str(e)[:140])); continue
        if not vids:
            print('EMPTY %s' % label); continue
        for r, v in enumerate(vids, 1):
            v['rank'] = r
        for v in pool:
            hist[v['videoId']] = v['views']
        data['themes'].append({'key': key, 'label': label, 'note': note,
                               'videos': vids, 'asof': data['updated'], 'stale': False})
        added.append('%s(%d)' % (label, len(vids)))
    # THEMES の並び順に合わせ直す（総合が先頭・うっちーPの歌が末尾は維持）
    order = ['trend'] + [t[0] for t in THEMES] + [OWN_KEY]
    data['themes'].sort(key=lambda t: order.index(t['key']) if t['key'] in order else 999)
    write_json(DATA, data)
    write_json(HIST, hist)
    print('FILL-MISSING 追加=%s' % (added or 'なし'))


def hof_only():
    """殿堂入りキャッシュだけを埋める（初回の一括投入用）。HOF_PER_RUN ジャンルまで。"""
    if not API_KEY:
        print('FATAL: 環境変数 YT_API_KEY が未設定'); sys.exit(1)
    hofc, done = refresh_hof(load_hof(), HOF_PER_RUN)
    save_hof(hofc)
    print('HOF-ONLY 更新=%s / 保有=%d/12ジャンル' % (done or 'なし', len(hofc)))


if __name__ == '__main__':
    if '--render-only' in sys.argv:
        render_only()
    elif '--hof-only' in sys.argv:
        hof_only()
    elif '--fill-missing' in sys.argv:
        fill_missing()
    else:
        main()
