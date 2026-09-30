"""
google_clients.py — Search Console / GA4 Data API の OAuth 認証とクライアント生成。

認証情報はハードコードしない。以下の環境変数（tools/.env で設定可）から読む。

    GOOGLE_OAUTH_CLIENT_SECRETS  … Google Cloud からダウンロードした client_secret_*.json のパス
    GOOGLE_OAUTH_TOKEN           … 発行済みトークンの保存先（既定: tools/.gsc_token.json）
    GSC_SITE_URL                 … Search Console のプロパティ（例: sc-domain:example.com）
    GA4_PROPERTY_ID              … GA4 プロパティID（数字のみ。例: 123456789）

初回のみブラウザが開き、Googleアカウントでの同意が必要（＝実行者の作業）。
2回目以降はトークンが自動更新されるため無人実行できる。
手順の詳細は tools/README.md「Google API のセットアップ」を参照。

サービスアカウントを使う場合（サーバー無人運用向け）
    GOOGLE_APPLICATION_CREDENTIALS にサービスアカウント鍵のパスを設定すると、
    OAuth ではなくサービスアカウントを使う。GA4 は該当サービスアカウントのメールを
    プロパティの閲覧者に追加、Search Console も同メールをプロパティのユーザーに追加すること。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from common import TOOLS_DIR, ToolError, require_env

LOG = logging.getLogger("affiliate.tools.google")

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]

DEFAULT_TOKEN_PATH = TOOLS_DIR / ".gsc_token.json"


def _import_or_die(module: str, package_hint: str) -> Any:
    try:
        return __import__(module, fromlist=["*"])
    except ImportError as exc:
        raise ToolError(
            f"{module} が未インストールです。`pip install {package_hint}` "
            f"（または `pip install -r tools/requirements.txt`）を実行してください。"
        ) from exc


def get_credentials(interactive: bool = True, token_path: Optional[str] = None) -> Any:
    """OAuth 認証情報を返す。トークンが無ければブラウザで同意フローを開始する。"""
    sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if sa_path:
        if not Path(sa_path).exists():
            raise ToolError(
                f"GOOGLE_APPLICATION_CREDENTIALS のファイルが見つかりません: {sa_path}"
            )
        service_account = _import_or_die(
            "google.oauth2.service_account", "google-auth"
        )
        LOG.info("サービスアカウントで認証します: %s", sa_path)
        return service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)

    google_requests = _import_or_die("google.auth.transport.requests", "google-auth")
    oauth_creds = _import_or_die("google.oauth2.credentials", "google-auth")

    tpath = Path(token_path or os.environ.get("GOOGLE_OAUTH_TOKEN") or DEFAULT_TOKEN_PATH)
    creds = None
    if tpath.exists():
        try:
            creds = oauth_creds.Credentials.from_authorized_user_file(str(tpath), SCOPES)
            LOG.debug("保存済みトークンを読み込みました: %s", tpath)
        except Exception as exc:
            LOG.warning("保存済みトークンを読めませんでした（再認証します）: %s: %s", tpath, exc)
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            LOG.info("アクセストークンを更新します")
            creds.refresh(google_requests.Request())
            _save_token(tpath, creds)
            return creds
        except Exception as exc:
            LOG.warning("トークン更新に失敗しました。再認証します: %s", exc)
            creds = None

    if not interactive:
        raise ToolError(
            f"有効なトークンがありません（{tpath}）。\n"
            f"  一度だけ手動で `python tools/cannibalization.py --auth-only` を実行し、"
            f"ブラウザで同意してください。"
        )

    flow_mod = _import_or_die("google_auth_oauthlib.flow", "google-auth-oauthlib")
    secrets = require_env(
        "GOOGLE_OAUTH_CLIENT_SECRETS",
        "Google Cloud Console でダウンロードした client_secret_*.json のパスを指定します。",
    )
    if not Path(secrets).exists():
        raise ToolError(f"クライアントシークレットのファイルが見つかりません: {secrets}")

    LOG.info("ブラウザで Google の同意画面を開きます（初回のみ）")
    try:
        flow = flow_mod.InstalledAppFlow.from_client_secrets_file(secrets, SCOPES)
        try:
            creds = flow.run_local_server(port=0, prompt="consent")
        except Exception as exc:
            LOG.warning("ローカルサーバ方式に失敗しました（%s）。コンソール方式に切り替えます。", exc)
            creds = flow.run_console()  # type: ignore[attr-defined]
    except Exception as exc:
        raise ToolError(f"OAuth 認証フローに失敗しました: {exc}") from exc

    _save_token(tpath, creds)
    return creds


def _save_token(path: Path, creds: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows では効かないことがある
        LOG.info("トークンを保存しました: %s（.gitignore 対象。共有しないこと）", path)
    except OSError as exc:
        LOG.error("トークンの保存に失敗しました: %s: %s", path, exc)


def build_search_console(creds: Any) -> Any:
    discovery = _import_or_die("googleapiclient.discovery", "google-api-python-client")
    try:
        return discovery.build("searchconsole", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        raise ToolError(f"Search Console クライアントの生成に失敗しました: {exc}") from exc


def build_ga4(creds: Any) -> Any:
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient  # type: ignore
    except ImportError as exc:
        raise ToolError(
            "google-analytics-data が未インストールです。"
            "`pip install google-analytics-data` を実行してください。"
        ) from exc
    try:
        return BetaAnalyticsDataClient(credentials=creds)
    except Exception as exc:
        raise ToolError(f"GA4 クライアントの生成に失敗しました: {exc}") from exc


def gsc_query(
    service: Any,
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: list[str],
    row_limit: int = 25000,
    search_type: str = "web",
    dimension_filter_groups: Optional[list[dict[str, Any]]] = None,
    max_rows: int = 100000,
) -> list[dict[str, Any]]:
    """Search Console Search Analytics をページングしながら全件取得する。"""
    rows: list[dict[str, Any]] = []
    start_row = 0
    while start_row < max_rows:
        body: dict[str, Any] = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimensions,
            "rowLimit": min(row_limit, max_rows - start_row),
            "startRow": start_row,
            "type": search_type,
        }
        if dimension_filter_groups:
            body["dimensionFilterGroups"] = dimension_filter_groups
        try:
            resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        except Exception as exc:
            detail = _explain_google_error(exc, site_url)
            raise ToolError(f"Search Console API の呼び出しに失敗しました: {detail}") from exc

        batch = resp.get("rows", [])
        rows += batch
        LOG.debug("GSC: startRow=%s で %s 行取得（累計 %s）", start_row, len(batch), len(rows))
        if len(batch) < body["rowLimit"]:
            break
        start_row += len(batch)
    LOG.info("Search Console から %s 行取得しました (%s〜%s, dims=%s)",
             len(rows), start_date, end_date, ",".join(dimensions))
    return rows


def _explain_google_error(exc: Exception, site_url: str) -> str:
    text = str(exc)
    if "403" in text:
        return (f"{text}\n  → API が有効化されていないか、{site_url} に対する権限がありません。"
                f" Google Cloud で Search Console API を有効化し、"
                f"Search Console 側で当該アカウントに権限があるか確認してください。")
    if "404" in text:
        return (f"{text}\n  → プロパティ {site_url} が見つかりません。"
                f" GSC_SITE_URL の形式を確認してください"
                f"（ドメインプロパティは 'sc-domain:example.com'、"
                f"URLプレフィックスは 'https://example.com/' で末尾スラッシュ必須）。")
    if "429" in text or "quota" in text.lower():
        return f"{text}\n  → APIクォータ超過です。時間をおいて再実行してください。"
    return text
