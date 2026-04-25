// PoC品質: 本番利用前にCSPポリシーをアプリ要件に合わせて調整すること
// CloudFront Function (Runtime: cloudfront-js-2.0)
// イベント: viewer-response
// 役割: すべてのレスポンスにセキュリティヘッダーを付与
// Lambda@Edgeより1/6以下のコスト・サブミリ秒実行でセキュリティヘッダー処理に最適

function handler(event) {
    var response = event.response;
    var headers  = response.headers;

    // HSTS: HTTPSを強制（1年間・サブドメイン含む・preloadリスト登録）
    headers["strict-transport-security"] = {
        value: "max-age=63072000; includeSubDomains; preload"
    };

    // MIMEスニッフィング防止
    headers["x-content-type-options"] = { value: "nosniff" };

    // クリックジャッキング防止（iframeへの埋め込みを禁止）
    headers["x-frame-options"] = { value: "DENY" };

    // XSSフィルター（レガシーブラウザ向け）
    headers["x-xss-protection"] = { value: "1; mode=block" };

    // リファラー情報の制御（同一オリジンはフルURL、クロスオリジンはoriginのみ送信）
    headers["referrer-policy"] = { value: "strict-origin-when-cross-origin" };

    // ブラウザ機能のアクセス制限
    headers["permissions-policy"] = {
        value: "camera=(), microphone=(), geolocation=(), payment=()"
    };

    // CSP: コンテンツセキュリティポリシー
    // 注意: アプリで外部リソース（CDN・フォント・分析ツール等）を使用する場合は
    //       対象ドメインをそれぞれのディレクティブに追加すること
    headers["content-security-policy"] = {
        value: [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",  // インラインスタイルが必要な場合
            "img-src 'self' data: https:",
            "font-src 'self'",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "upgrade-insecure-requests"
        ].join("; ")
    };

    // Originポリシー分離（Spectreなどのサイドチャネル攻撃対策）
    headers["cross-origin-opener-policy"]   = { value: "same-origin" };
    headers["cross-origin-embedder-policy"] = { value: "require-corp" };
    headers["cross-origin-resource-policy"] = { value: "same-origin" };

    return response;
}
