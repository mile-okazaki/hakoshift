# hakoshift

配達チームのための業務アプリ集。単一HTMLファイル + Firebase 構成で、サーバー不要でホスティングできます。

| アプリ | ファイル | 説明 |
|---|---|---|
| 🚚 ハコシフ | `index.html` | 配達シフト管理（本番版） |
| 🚚 ハコシフ【デモ】 | `index-preview.html` | ログイン不要のデモ版 |
| 🎓 ハコマナ | `learn.html` | 研修・教育プラットフォーム（本番版） |
| 🎓 ハコマナ【デモ】 | `learn-preview.html` | ログイン不要のデモ版（サンプルデータ入り） |

---

## 🎓 ハコマナ — 研修・教育プラットフォーム

オンライン講座プラットフォーム「オンクラス」を参考にした研修・教育用のプラットフォームです。配達チームに限らず、**社内研修・スクール・講座運営などあらゆる教育用途**に使えます。

### 主な機能

**講師（管理者）**
- **プラットフォーム設定（ホワイトレーベル）**：プラットフォーム名・サブタイトル・テーマカラー・会社ロゴを自由にカスタマイズ。ログイン画面・ヘッダー・修了証・ブラウザタブまで全体に反映され、自社ブランドのプラットフォームとして運用できます
- コース作成（アイコン・カラー・公開/非公開・招待コード）、**カテゴリ分類**（コース一覧で絞り込み）、**アーカイブ**（終了した研修をデータを残したまま受講生から非表示に）
- 章（チャプター）→ レッスンのカリキュラム構成、並び替え
- **順次学習モード**：前のレッスンを完了するまで次のレッスンをロック（コース設定でON/OFF）
- レッスンの**下書き**（受講生に非表示・進捗計算からも除外）と**所要時間の目安**（「約◯分」表示）
- レッスン編集：テキスト記入（見出し・箇条書き・強調・自動リンク）／YouTube・Vimeo動画埋め込み／外部リンク／ファイル添付（Firebase Storage）
- **教材・資料一覧**：コース内のファイル・リンクをコースページに自動で集約表示（受講生はワンタップでダウンロード）
- **理解度テスト**：単一選択・複数選択・○×・記述式問題、合格ライン設定、解説、自動採点（記述式は講師が○×確認）、シャッフル出題（丸暗記対策）
- **設問別の正答率分析**：設問ごとの正答率と選択肢の回答分布を表示。正答率最低の設問に「要注意」表示
- **課題**：受講生のテキスト提出にフィードバック
- **進捗管理**：受講生×レッスンの進捗マトリクス、テストのベストスコア、CSV出力
- 受講生管理（講師権限の付与/剥奪、個人別の学習状況）
- お知らせ配信（ピン留め・未読NEWバッジ対応）
- 受講生の質問へのコメント返信
- **修了証**：コース100%修了で受講生が修了証を表示・印刷（PDF保存）可能（コース設定でON/OFF）
- **受講期限**：コースに期限日を設定でき、受講生に「あと◯日」「期限超過」を表示
- **受講生の割当**：進捗管理画面から受講生をコースに一括追加・受講解除（必須研修の割当に）
- ホームにコース別の平均進捗サマリーを表示、初回はセットアップガイドを表示
- **データ管理**：全データのJSONエクスポート（バックアップ）・インポート（復元・環境移行）

**受講生**
- コース受講（公開コースへの参加／招待コードでの参加）
- レッスン学習と完了チェック、進捗バー表示（順次学習コースではロック表示）
- 理解度テスト受験（合格でレッスン自動完了、再挑戦可）
- 課題のテキスト提出・講師フィードバック閲覧
- レッスンごとの質問・コメント投稿
- 学習状況ページで自分の進捗・テスト結果を確認
- コース修了時の**修了証**表示・印刷、**連続学習日数**（ストリーク）の表示
- 期限が近い/超過したコースの警告表示

### ロールについて

- **最初に登録したユーザーが自動的に講師（管理者）**になります
- 2人目以降は受講生として登録されます
- 講師は「受講生」ページから他のユーザーに講師権限を付与できます

### デモ版の使い方

`learn-preview.html` をブラウザで開くだけで動きます（ログイン不要）。
- サンプルの研修コース・受講生・テスト結果・課題提出が入っています
- 上部バーの「視点切替」で講師⇔受講生の画面を切り替えられます
- データは端末のlocalStorageにのみ保存され、「ログアウト」で初期状態に戻ります

### 本番版のセットアップ

`learn.html` はハコシフと同じFirebaseプロジェクトを使用し、`edu_` プレフィックスのコレクションでデータを分離しています。デプロイ前に以下の設定が必要です。

#### 1. Firestore セキュリティルール

Firebase Console → Firestore Database → ルール に、**既存のハコシフのルールに追記**する形で以下を追加してください。

```
    // ===== ハコマナ（教育プラットフォーム） =====
    function eduSignedIn() { return request.auth != null; }
    function isEduInstructor() {
      return eduSignedIn() &&
        get(/databases/$(database)/documents/edu_users/$(request.auth.uid)).data.role == 'instructor';
    }
    match /edu_users/{uid} {
      allow read: if eduSignedIn();
      allow create: if eduSignedIn() && request.auth.uid == uid;
      allow update: if eduSignedIn() && (request.auth.uid == uid || isEduInstructor());
    }
    match /edu_courses/{id} {
      allow read: if eduSignedIn();
      allow write: if isEduInstructor();
    }
    match /edu_lessons/{id} {
      allow read: if eduSignedIn();
      allow write: if isEduInstructor();
    }
    match /edu_announcements/{id} {
      allow read: if eduSignedIn();
      allow write: if isEduInstructor();
    }
    match /edu_enrollments/{id} {
      allow read: if eduSignedIn();
      allow create, update: if eduSignedIn() && (request.resource.data.uid == request.auth.uid || isEduInstructor());
      allow delete: if isEduInstructor();
    }
    match /edu_quiz_results/{id} {
      allow read: if eduSignedIn();
      allow create, update: if eduSignedIn() && (request.resource.data.uid == request.auth.uid || isEduInstructor());
      allow delete: if isEduInstructor();
    }
    match /edu_submissions/{id} {
      allow read: if eduSignedIn();
      allow create, update: if eduSignedIn() && (request.resource.data.uid == request.auth.uid || isEduInstructor());
      allow delete: if isEduInstructor();
    }
    match /edu_comments/{id} {
      allow read: if eduSignedIn();
      allow create: if eduSignedIn() && request.resource.data.uid == request.auth.uid;
      allow delete: if eduSignedIn() && (resource.data.uid == request.auth.uid || isEduInstructor());
    }
    match /edu_settings/{id} {
      // ブランド設定はログイン画面でも表示するため誰でも読み取り可（機密情報は保存しないこと）
      allow read: if true;
      allow write: if isEduInstructor();
    }
```

> メモ: ログインユーザーであれば読み取り可能な「社内利用向け」のルールです。社外公開する場合はより厳密なルール設計を検討してください。

#### 2. Firebase Storage（ファイル添付を使う場合）

Firebase Console → Storage を有効化し、ルールに以下を追加してください。

```
    match /edu_uploads/{allPaths=**} {
      allow read: if request.auth != null;
      allow write: if request.auth != null && request.resource.size < 20 * 1024 * 1024;
    }
```

Storageを使わない場合も、リンクブロック（Google ドライブ等のURL）で資料共有ができます。

#### 3. Authentication

ハコシフで設定済みであれば追加設定は不要です（メール/パスワード認証とGoogleログインを使用）。

#### 本番公開チェックリスト

- [ ] Firestoreルールに `edu_` 用ルールを追記した（上記1）
- [ ] Storageを有効化しルールを追加した（ファイル添付を使う場合・上記2）
- [ ] `learn.html` をホスティングに配置し、ログイン→講師アカウントが作成されることを確認した（**最初に登録した人が講師**になります）
- [ ] 設定 → プラットフォーム設定で名称・ロゴ・テーマカラーを自社仕様にした（プロプラン以上）
- [ ] テスト用コースを1つ作成し、別アカウント（受講生）で受講→テスト→修了の流れを確認した
- [ ] 設定 → データ管理から初回バックアップを取得した
- [ ] （販売する場合）`PLANS` の価格と `PLAN_CONTACT_URL` を自社の決済リンクに変更した

### 有料プランの運用（販売者向け）

プラットフォームには3段階のプラン体系が組み込まれています。設定 → 「ご利用プラン」で確認できます。

| | フリー | プロ | ビジネス |
|---|---|---|---|
| 料金（初期設定） | ¥0 | 月額 ¥9,800 | 月額 ¥29,800 |
| 公開コース数 | 3件 | 無制限 | 無制限 |
| 受講生数 | 30人 | 300人 | 無制限 |
| テスト・課題・進捗・修了証 | ○ | ○ | ○ |
| ホワイトレーベル（名称/ロゴ/配色） | × | ○ | ○ |

**運用方法**
1. 価格・購入導線は `learn.html` 内の `PLANS` / `PLAN_CONTACT_URL` 定数を書き換えてください（Stripe決済リンクや申込フォームのURLを設定可能）
2. 購入者には**ライセンスキー**を発行します。キーは `HAKO-PRO-数字4桁-チェックサム2桁`（プロ）/ `HAKO-BIZ-...`（ビジネス）形式で、以下のNode.jsスニペットで生成できます：

```js
function genKey(type, num) { // type: 'PRO' | 'BIZ', num: '0001'〜'9999'
  const body = `HAKO-${type}-${num}`;
  let s = 0; for (let i = 0; i < body.length; i++) s += body.charCodeAt(i) * (i + 1);
  return `${body}-${((s * 7 + 13) % 90) + 10}`;
}
```

3. 購入者が設定画面でキーを入力すると、その場でプランが有効化されます

> メモ: キー検証はクライアントサイドの簡易な仕組みです（ビルド不要・サーバー不要の構成を保つため）。厳密なライセンス管理や自動課金が必要な段階になったら、Stripe + サーバー側検証への移行をご検討ください。

### 技術構成

- React 18（UMD）+ Babel Standalone 7 + Tailwind CSS（CDN）
- Firebase compat SDK 10.14.1（Auth / Firestore / Storage）
- ビルド不要・単一HTMLファイル。GitHub Pages等の静的ホスティングにそのまま配置できます
