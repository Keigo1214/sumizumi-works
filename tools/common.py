"""
common.py — Phase 0-D 運用自動化スクリプト群の共通基盤。

提供するもの:
  * ロギング設定（失敗を握りつぶさない方針）
  * 設定ファイル（YAML）と .env の読み込み
  * PoliteFetcher: robots.txt 尊重・UA明示・レート制限・逐次アクセスのHTTP取得
  * 数値の表記ゆれ正規化と抽出（1,000 / 1000 / 千 / 円 / ￥ / % / ％ / 全角半角）
  * HTML → プレーンテキスト変換

このモジュール単体では外部APIを叩かない。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.robotparser
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse

LOG = logging.getLogger("affiliate.tools")

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TOOLS_DIR.parent
DEFAULT_CONFIG_PATH = TOOLS_DIR / "config.yaml"


# --------------------------------------------------------------------------
# ロギング
# --------------------------------------------------------------------------

def setup_logging(verbose: bool = False, logfile: Optional[str] = None) -> None:
    """標準エラーへ出力。verbose で DEBUG。logfile 指定時はファイルにも出す。"""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    # 依存ライブラリの過剰なログを抑制
    for noisy in ("urllib3", "googleapiclient", "google", "google_auth_httplib2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class ToolError(Exception):
    """このツール群が意図的に投げる致命的エラー。main で捕捉してメッセージを出す。"""


def die(message: str, code: int = 1) -> None:
    LOG.error(message)
    sys.exit(code)


# --------------------------------------------------------------------------
# 設定 / 環境変数
# --------------------------------------------------------------------------

def load_env(dotenv_path: Optional[str] = None) -> None:
    """.env を読み込む。python-dotenv が無い場合は自前の簡易パーサで読む。

    既に環境変数に値がある場合はそちらを優先する（本番の環境変数を上書きしない）。
    """
    path = Path(dotenv_path) if dotenv_path else (TOOLS_DIR / ".env")
    if not path.exists():
        LOG.debug(".env が見つかりません (%s)。環境変数のみを使用します。", path)
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path, override=False)
        LOG.debug(".env を読み込みました: %s", path)
        return
    except ImportError:
        LOG.debug("python-dotenv が未インストールのため簡易パーサを使用します。")

    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    except OSError as exc:
        raise ToolError(f".env の読み込みに失敗しました: {path}: {exc}") from exc


def require_env(name: str, hint: str = "") -> str:
    """必須の環境変数を取得。無ければ何が足りないか明示して落とす。"""
    value = os.environ.get(name)
    if not value:
        msg = f"環境変数 {name} が設定されていません。"
        if hint:
            msg += f" {hint}"
        msg += f" tools/.env.example を tools/.env にコピーして設定してください。"
        raise ToolError(msg)
    return value


def load_config(path: Optional[str] = None) -> dict[str, Any]:
    """YAML 設定を読み込む。存在しない場合は分かりやすいエラーを出す。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ToolError(
            f"設定ファイルが見つかりません: {cfg_path}\n"
            f"  tools/config.example.yaml を tools/config.yaml にコピーして編集してください。"
        )
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ToolError(
            "PyYAML が未インストールです。`pip install -r tools/requirements.txt` を実行してください。"
        ) from exc
    try:
        with cfg_path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # yaml.YAMLError と OSError の両方
        raise ToolError(f"設定ファイルの読み込みに失敗しました: {cfg_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolError(f"設定ファイルのトップレベルはマッピングである必要があります: {cfg_path}")
    LOG.debug("設定を読み込みました: %s", cfg_path)
    return data


def cfg_get(cfg: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """'fetch.min_interval_sec' のようなドット区切りで設定値を取得。"""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def expand_path(value: str) -> Path:
    """設定中のパスを絶対パス化（相対は tools/ 基準）。~ と環境変数も展開。"""
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    p = Path(expanded)
    return p if p.is_absolute() else (TOOLS_DIR / p).resolve()


# --------------------------------------------------------------------------
# HTTP 取得（礼儀正しく）
# --------------------------------------------------------------------------

@dataclasses.dataclass
class FetchResult:
    url: str                      # 最終URL（リダイレクト後）
    requested_url: str            # 要求したURL
    status: int
    text: str
    ok: bool
    error: Optional[str] = None
    redirected: bool = False
    redirect_chain: list[str] = dataclasses.field(default_factory=list)
    content_type: str = ""

    @property
    def is_redirect(self) -> bool:
        return self.redirected


_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w\-]+)""", re.I)
_HEADER_CHARSET_RE = re.compile(r"charset\s*=\s*([\w\-]+)", re.I)


def _decode_body(resp: Any, content: bytes) -> str:
    """レスポンスボディを正しい文字コードで復号する。

    requests は Content-Type ヘッダに charset が無い text/* を HTTP仕様どおり
    ISO-8859-1 と解釈する。しかし日本語サイトには charset をヘッダに出さず
    HTML の <meta charset> だけで宣言しているものが多く、そのままでは
    全文字化けする（料金も社名も比較不能になる）。
    優先順位: ヘッダのcharset > BOM > <meta charset> > 自動判定 > utf-8
    """
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig", errors="replace")

    candidates: list[str] = []
    header_match = _HEADER_CHARSET_RE.search(resp.headers.get("Content-Type", "") or "")
    if header_match:
        candidates.append(header_match.group(1))
    else:
        meta_match = _META_CHARSET_RE.search(content[:4096])
        if meta_match:
            try:
                candidates.append(meta_match.group(1).decode("ascii"))
            except UnicodeDecodeError:
                pass
        try:
            if resp.apparent_encoding:
                candidates.append(resp.apparent_encoding)
        except Exception as exc:
            LOG.debug("文字コードの自動判定に失敗しました: %s", exc)
    candidates.append("utf-8")

    for encoding in candidates:
        try:
            return content.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            LOG.debug("文字コード %s では復号できませんでした", encoding)
    return content.decode("utf-8", errors="replace")


class PoliteFetcher:
    """robots.txt を尊重し、ホストごとに間隔を空けて逐次アクセスするフェッチャ。

    - User-Agent を明示（config で設定、連絡先URLを含めることを推奨）
    - 同一ホストへの並列アクセスをしない（このクラスは同期・逐次のみ）
    - robots.txt の Crawl-delay があれば min_interval より優先（大きい方を採用）
    - robots.txt で拒否されたURLは取得しない（--ignore-robots で明示的に上書き可能）
    """

    def __init__(
        self,
        user_agent: str,
        min_interval_sec: float = 3.0,
        timeout_sec: float = 20.0,
        max_retries: int = 2,
        respect_robots: bool = True,
        max_bytes: int = 5_000_000,
    ) -> None:
        try:
            import requests  # type: ignore
        except ImportError as exc:
            raise ToolError(
                "requests が未インストールです。`pip install -r tools/requirements.txt` を実行してください。"
            ) from exc
        self._requests = requests
        self.user_agent = user_agent
        self.min_interval_sec = float(min_interval_sec)
        self.timeout_sec = float(timeout_sec)
        self.max_retries = int(max_retries)
        self.respect_robots = bool(respect_robots)
        self.max_bytes = int(max_bytes)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en;q=0.8",
            }
        )
        self._last_request_at: dict[str, float] = {}
        self._robots_cache: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._crawl_delay: dict[str, float] = {}

    # -- robots -------------------------------------------------------------

    def _robots_for(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        parsed = urlparse(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        if host_key in self._robots_cache:
            return self._robots_cache[host_key]

        robots_url = urljoin(host_key + "/", "robots.txt")
        parser: Optional[urllib.robotparser.RobotFileParser] = None
        try:
            self._throttle(parsed.netloc)
            resp = self.session.get(robots_url, timeout=self.timeout_sec)
            self._last_request_at[parsed.netloc] = time.monotonic()
            if resp.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(resp.text.splitlines())
                delay = parser.crawl_delay(self.user_agent)
                if delay:
                    self._crawl_delay[parsed.netloc] = float(delay)
                    LOG.info("robots.txt の Crawl-delay=%ss を採用します (%s)", delay, parsed.netloc)
            elif resp.status_code in (401, 403):
                # 仕様上は全面拒否と解釈するのが安全側
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
                LOG.warning("robots.txt が %s を返しました。全面禁止として扱います (%s)",
                            resp.status_code, host_key)
            else:
                LOG.debug("robots.txt なし (status=%s): %s → 制限なしとして扱います",
                          resp.status_code, robots_url)
        except Exception as exc:
            LOG.warning("robots.txt の取得に失敗しました (%s): %s → 制限なしとして扱います",
                        robots_url, exc)
        self._robots_cache[host_key] = parser
        return parser

    def can_fetch(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception as exc:  # 壊れた robots.txt
            LOG.warning("robots.txt の判定に失敗しました (%s): %s → 許可として扱います", url, exc)
            return True

    # -- throttle -----------------------------------------------------------

    def _throttle(self, netloc: str) -> None:
        interval = max(self.min_interval_sec, self._crawl_delay.get(netloc, 0.0))
        last = self._last_request_at.get(netloc)
        if last is None:
            return
        elapsed = time.monotonic() - last
        if elapsed < interval:
            wait = interval - elapsed
            LOG.debug("レート制限のため %.2f 秒待機します (%s)", wait, netloc)
            time.sleep(wait)

    # -- fetch --------------------------------------------------------------

    def fetch(self, url: str) -> FetchResult:
        """URLを取得。例外は投げず FetchResult.ok / error で返す（呼び出し側で集計するため）。"""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return FetchResult(url, url, 0, "", False,
                               error=f"http/https 以外のURLはサポートしません: {url}")

        if self.respect_robots and not self.can_fetch(url):
            LOG.warning("robots.txt により取得を見送りました: %s", url)
            return FetchResult(url, url, 0, "", False, error="robots.txt により禁止されています")

        last_error: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            self._throttle(parsed.netloc)
            try:
                resp = self.session.get(
                    url, timeout=self.timeout_sec, allow_redirects=True, stream=True
                )
                self._last_request_at[parsed.netloc] = time.monotonic()

                content = b""
                for chunk in resp.iter_content(chunk_size=65536):
                    content += chunk
                    if len(content) > self.max_bytes:
                        LOG.warning("本文が %s バイトを超えたため打ち切りました: %s",
                                    self.max_bytes, url)
                        break
                resp.close()

                text = _decode_body(resp, content)

                chain = [r.url for r in resp.history]
                redirected = bool(chain) or (resp.url.rstrip("/") != url.rstrip("/"))

                if resp.status_code >= 500 and attempt < self.max_retries:
                    last_error = f"HTTP {resp.status_code}"
                    backoff = 2.0 * (attempt + 1)
                    LOG.warning("HTTP %s: %s → %.1f 秒後に再試行 (%s/%s)",
                                resp.status_code, url, backoff, attempt + 1, self.max_retries)
                    time.sleep(backoff)
                    continue

                return FetchResult(
                    url=resp.url,
                    requested_url=url,
                    status=resp.status_code,
                    text=text,
                    ok=resp.status_code < 400,
                    error=None if resp.status_code < 400 else f"HTTP {resp.status_code}",
                    redirected=redirected,
                    redirect_chain=chain,
                    content_type=resp.headers.get("Content-Type", ""),
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._last_request_at[parsed.netloc] = time.monotonic()
                if attempt < self.max_retries:
                    backoff = 2.0 * (attempt + 1)
                    LOG.warning("取得失敗 (%s): %s → %.1f 秒後に再試行 (%s/%s)",
                                url, last_error, backoff, attempt + 1, self.max_retries)
                    time.sleep(backoff)
                else:
                    LOG.error("取得に失敗しました (%s): %s", url, last_error)

        return FetchResult(url, url, 0, "", False, error=last_error or "不明なエラー")


def build_fetcher(cfg: dict[str, Any], ignore_robots: bool = False) -> PoliteFetcher:
    fetch_cfg = cfg.get("fetch", {}) or {}
    ua = fetch_cfg.get("user_agent")
    if not ua:
        raise ToolError(
            "config.yaml の fetch.user_agent が未設定です。"
            "サイト名と連絡先URLを含む User-Agent を設定してください。"
        )
    if ignore_robots:
        LOG.warning("--ignore-robots が指定されました。robots.txt を無視します。"
                    "自社サイトなど正当な理由がある場合のみ使用してください。")
    return PoliteFetcher(
        user_agent=ua,
        min_interval_sec=fetch_cfg.get("min_interval_sec", 3.0),
        timeout_sec=fetch_cfg.get("timeout_sec", 20.0),
        max_retries=fetch_cfg.get("max_retries", 2),
        respect_robots=not ignore_robots and fetch_cfg.get("respect_robots", True),
        max_bytes=fetch_cfg.get("max_bytes", 5_000_000),
    )


# --------------------------------------------------------------------------
# HTML → テキスト
# --------------------------------------------------------------------------

_SCRIPT_TAGS = ("script", "style", "noscript", "template", "svg", "iframe")


def html_to_text(html: str, selector: Optional[str] = None) -> str:
    """HTML を可読テキストに変換。selector 指定時はその範囲のみ。

    beautifulsoup4 が無い場合は正規表現のフォールバック（精度は落ちる）。
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        LOG.warning("beautifulsoup4 が未インストールのため簡易HTML除去にフォールバックします。"
                    "精度が落ちます。`pip install -r tools/requirements.txt` を推奨します。")
        stripped = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
        stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
        return normalize_whitespace(unescape_entities(stripped))

    parser = "lxml"
    try:
        soup = BeautifulSoup(html, parser)
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup(list(_SCRIPT_TAGS)):
        tag.decompose()

    root = soup
    if selector:
        nodes = soup.select(selector)
        if not nodes:
            raise ToolError(f"セレクタ '{selector}' に一致する要素がページ内に見つかりませんでした")
        text = "\n".join(n.get_text("\n", strip=True) for n in nodes)
        return normalize_whitespace(text)

    text = root.get_text("\n", strip=True)
    return normalize_whitespace(text)


def unescape_entities(text: str) -> str:
    import html as html_mod

    return html_mod.unescape(text)


def normalize_whitespace(text: str) -> str:
    """行内の空白を潰し、空行の連続を1つにまとめる。"""
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t　\xa0]+", " ", raw).strip()
        lines.append(line)
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(line)
    return "\n".join(out).strip()


def strip_markdown(md: str) -> str:
    """Markdown の記法を軽く落として本文テキストにする（数値抽出のため）。"""
    text = re.sub(r"(?s)```.*?```", " ", md)          # コードブロック
    text = re.sub(r"`[^`]*`", " ", text)              # インラインコード
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # 画像
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # リンク → テキストのみ
    text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]*", "", text, flags=re.M)  # 見出し
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)  # 強調
    return text


# --------------------------------------------------------------------------
# 数値の正規化と抽出
# --------------------------------------------------------------------------

# 桁の接尾辞（漢数字の万進法 + 英語の k/M/B）
MAGNITUDE: dict[str, Decimal] = {
    "千": Decimal(1_000),
    "万": Decimal(10_000),
    "億": Decimal(100_000_000),
    "兆": Decimal(1_000_000_000_000),
    "k": Decimal(1_000),
    "K": Decimal(1_000),
    "M": Decimal(1_000_000),
    "B": Decimal(1_000_000_000),
}

# 単位を意味カテゴリに寄せる（円 / ￥ / ¥ は同じ、% と ％ は同じ）
UNIT_ALIASES: dict[str, str] = {
    "円": "JPY", "¥": "JPY", "￥": "JPY", "元": "JPY",
    "%": "PCT", "％": "PCT", "パーセント": "PCT", "ポイント": "PCT",
    "ドル": "USD", "$": "USD", "＄": "USD", "USD": "USD",
    "人": "PEOPLE", "名": "PEOPLE", "社": "COMPANY", "件": "COUNT", "割": "WARI",
    "台": "COUNT", "個": "COUNT", "本": "COUNT", "回": "COUNT",
    "GB": "GB", "MB": "MB", "TB": "TB",
    "時間": "HOUR", "分": "MINUTE", "秒": "SECOND",
    "日": "DAY", "年": "YEAR", "ヶ月": "MONTH", "か月": "MONTH",
    "カ月": "MONTH", "ケ月": "MONTH", "箇月": "MONTH", "月": "MONTH",
}

_UNIT_PATTERN = "|".join(
    sorted((re.escape(u) for u in UNIT_ALIASES), key=len, reverse=True)
)
_MAG_PATTERN = "|".join(sorted((re.escape(m) for m in MAGNITUDE), key=len, reverse=True))

NUMBER_RE = re.compile(
    r"(?P<currency_prefix>[¥$])?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.(?P<frac>\d+))?"
    r"\s*(?P<mag>" + _MAG_PATTERN + r")?"
    r"\s*(?P<unit>" + _UNIT_PATTERN + r")?"
)


def normalize_text(text: str) -> str:
    """全角→半角などの文字レベル正規化。

    NFKC により ０-９→0-9、％→%、￥→¥、，→, が揃う。
    その後、数字の区切りとして使われがちな空白を除去する。
    """
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace(" ", " ").replace("　", " ")
    # 見えない書式文字（ゼロ幅スペース U+200B・ソフトハイフン U+00AD・BOM U+FEFF など Unicode の Cf 類）を除く。
    # 数値の間に入ると、読者には「12,000社」と見えるのに「1」と「2,000社」に分かれてしまうため
    if any(unicodedata.category(ch) == "Cf" for ch in normalized):
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")
    return normalized


@dataclasses.dataclass(frozen=True)
class NumberToken:
    raw: str                 # 元の表記
    value: Decimal           # 正規化後の値（桁接尾辞を展開済み）
    unit: Optional[str]      # 正規化後の単位カテゴリ（JPY / PCT / None など）
    context: str = ""        # 前後の文脈（レポート用）

    def key(self) -> tuple[str, Optional[str]]:
        return (format_decimal(self.value), self.unit)

    def display(self) -> str:
        unit = f" [{self.unit}]" if self.unit else ""
        return f"{self.raw} (= {format_decimal(self.value)}{unit})"


def format_decimal(value: Decimal) -> str:
    """比較・表示用に Decimal を正規化した文字列にする（1000.0 と 1000 を同一視）。"""
    normalized = value.normalize()
    sign, digits, exponent = normalized.as_tuple()
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def extract_numbers(
    text: str,
    context_chars: int = 30,
    skip_year_like: bool = True,
    ignore_patterns: Optional[Iterable[str]] = None,
) -> list[NumberToken]:
    """テキストから数値トークンを抽出し、表記ゆれを正規化して返す。

    skip_year_like=True のとき、1900〜2100 の単位なし整数（西暦とみなす）を除外する。
    ignore_patterns にマッチする raw 表記は除外する。
    """
    return [tok for tok, _s, _e, _n in iter_number_matches(
        text, context_chars, skip_year_like, ignore_patterns)]


def iter_number_matches(
    text: str,
    context_chars: int = 30,
    skip_year_like: bool = True,
    ignore_patterns: Optional[Iterable[str]] = None,
) -> list[tuple["NumberToken", int, int, str]]:
    """extract_numbers と同じ規則で数値を抽出し、位置も返す。

    戻り値は (トークン, 開始位置, 終了位置, 正規化後のテキスト) のリスト。
    位置は「正規化後のテキスト」上の位置（NFKC で文字数が変わりうるため）。
    「1人あたり」の判定など、数値の直後の文字を見たい処理のために使う。
    """
    normalized = normalize_text(text)
    compiled_ignores = [re.compile(p) for p in (ignore_patterns or [])]
    tokens: list[tuple[NumberToken, int, int, str]] = []

    for m in NUMBER_RE.finditer(normalized):
        int_part = m.group("int").replace(",", "")
        frac = m.group("frac")
        mag = m.group("mag")
        unit_raw = m.group("unit")
        prefix = m.group("currency_prefix")

        try:
            value = Decimal(int_part + ("." + frac if frac else ""))
        except InvalidOperation:
            LOG.debug("数値としてパースできませんでした: %r", m.group(0))
            continue

        if mag:
            value *= MAGNITUDE[mag]

        unit: Optional[str] = None
        if unit_raw:
            unit = UNIT_ALIASES.get(unit_raw)
        elif prefix:
            unit = UNIT_ALIASES.get(prefix)
        elif mag in ("千", "万", "億", "兆"):
            # 「3万」のように単位が省略されるケースは単位不明として扱う
            unit = None

        raw = m.group(0).strip()
        if any(p.search(raw) for p in compiled_ignores):
            continue
        # 西暦の除外。「2026」だけでなく「2026年」も対象にする
        # （日付は記事にもページにも大量に現れ、照合してもノイズにしかならない）
        if skip_year_like and unit in (None, "YEAR") and not frac and not mag:
            if 1900 <= value <= 2100 and len(int_part) == 4:
                continue

        start = max(0, m.start() - context_chars)
        end = min(len(normalized), m.end() + context_chars)
        context = normalized[start:end].replace("\n", " ")

        tokens.append((NumberToken(raw=raw, value=value, unit=unit, context=context),
                       m.start(), m.end(), normalized))

    return tokens


def numbers_match(
    claim: NumberToken,
    page_tokens: Iterable[NumberToken],
    strict_units: bool = False,
    rel_tolerance: float = 0.0,
) -> Optional[NumberToken]:
    """記事側の数値が、参照ページ側の数値集合に存在するか。見つかればそのトークンを返す。

    strict_units=False の場合、ページ側の単位が取れていない数値も一致候補とする
    （HTMLではセル分割等で単位が離れることが多いため）。
    """
    for page_token in page_tokens:
        if not _values_close(claim.value, page_token.value, rel_tolerance):
            continue
        if claim.unit is None or page_token.unit is None:
            if strict_units and claim.unit != page_token.unit:
                continue
            return page_token
        if claim.unit == page_token.unit:
            return page_token
    return None


def _values_close(a: Decimal, b: Decimal, rel_tolerance: float) -> bool:
    if a == b:
        return True
    if rel_tolerance <= 0:
        return False
    if a == 0:
        return abs(b) <= Decimal(str(rel_tolerance))
    return abs((a - b) / a) <= Decimal(str(rel_tolerance))


# --------------------------------------------------------------------------
# 出力ユーティリティ
# --------------------------------------------------------------------------

def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    LOG.info("書き出しました: %s", path)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    LOG.info("書き出しました: %s", path)


def pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    """前期比（%）。前期が 0 または欠損なら None。"""
    if current is None or previous in (None, 0):
        return None
    return (current - float(previous)) / float(previous) * 100.0


def format_change(current: Optional[float], previous: Optional[float], invert: bool = False) -> str:
    """前期比を矢印付きで整形。invert=True は「小さいほど良い」指標（平均掲載順位など）。"""
    if current is None or previous is None:
        return "—"
    delta = current - previous
    change = pct_change(current, previous)
    if abs(delta) < 1e-9:
        return "→ ±0"
    improving = (delta < 0) if invert else (delta > 0)
    arrow = "▲" if improving else "▼"
    # 件数系は小数を出さない。CTRのような小さな差は桁を増やす
    if float(delta).is_integer():
        delta_s = f"{delta:+,.0f}"
    elif abs(delta) < 1:
        delta_s = f"{delta:+,.2f}"
    else:
        delta_s = f"{delta:+,.1f}"
    if change is None:
        return f"{arrow} {delta_s}"
    return f"{arrow} {delta_s} ({change:+.1f}%)"
