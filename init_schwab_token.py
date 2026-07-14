from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)


def _is_timeout_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    return "timeout" in name or "timed out" in text or "readtimeout" in text


def _token_file_has_access_token(token_path: Path) -> bool:
    if not token_path.exists():
        return False
    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    token = payload.get("token") if isinstance(payload.get("token"), dict) else payload
    return bool(token.get("access_token")) if isinstance(token, dict) else False


def _create_client_with_retries(
    schwab_module,
    api_key: str,
    app_secret: str,
    callback_url: str,
    token_path: Path,
    attempts: int = 2,
    retry_sec: float = 2.0,
):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return schwab_module.auth.client_from_manual_flow(
                api_key=api_key,
                app_secret=app_secret,
                callback_url=callback_url,
                token_path=str(token_path),
            )
        except Exception as exc:
            last_exc = exc
            if not _is_timeout_error(exc):
                raise

            # In some edge cases the token may be persisted despite a timeout.
            if _token_file_has_access_token(token_path):
                print("OAuth timed out, but a token file was detected. Using saved token...")
                return schwab_module.auth.client_from_token_file(
                    token_path=str(token_path),
                    api_key=api_key,
                    app_secret=app_secret,
                )

            if attempt < attempts:
                print(
                    f"OAuth attempt {attempt}/{attempts} timed out; "
                    f"retrying in {retry_sec:.1f}s..."
                )
                time.sleep(retry_sec)
                print("Please complete the browser login flow again.")
                continue

    if last_exc:
        raise last_exc
    raise RuntimeError("Unable to create Schwab OAuth client.")


def _verify_token_with_retries(client, attempts: int = 3, retry_sec: float = 2.0) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            resp = client.get_account_numbers()
            if resp.status_code == 200:
                print("Token verified successfully.")
                return True

            # Retry transient server-side issues; otherwise stop and report.
            if resp.status_code >= 500 and attempt < attempts:
                print(
                    f"Verification attempt {attempt}/{attempts} returned HTTP "
                    f"{resp.status_code}; retrying in {retry_sec:.1f}s..."
                )
                time.sleep(retry_sec)
                continue

            print(f"Token created, but account verification returned HTTP {resp.status_code}")
            return False
        except Exception as exc:
            if _is_timeout_error(exc) and attempt < attempts:
                print(
                    f"Verification attempt {attempt}/{attempts} timed out; "
                    f"retrying in {retry_sec:.1f}s..."
                )
                time.sleep(retry_sec)
                continue

            if _is_timeout_error(exc):
                print("Token created, but verification timed out after retries.")
                print("You can rerun this script, or continue and verify later in the app.")
                return False

            print(f"Token created, but verification failed: {exc}")
            return False

    return False


def main() -> None:
    try:
        import schwab
    except Exception as exc:
        sys.exit(f"schwab-py is required for token initialization: {exc}")

    api_key = os.environ.get("SCHWAB_API_KEY", "").strip()
    app_secret = os.environ.get("SCHWAB_APP_SECRET", "").strip().strip("'").strip('"')
    token_path = Path(os.environ.get("SCHWAB_TOKEN_PATH", ROOT / "schwab_token.json"))
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")

    if not api_key or not app_secret:
        sys.exit("Set SCHWAB_API_KEY and SCHWAB_APP_SECRET in .env first.")

    token_path.parent.mkdir(parents=True, exist_ok=True)

    print("Starting Schwab OAuth manual flow...")
    print(f"Token output path: {token_path}")
    print(f"Callback URL: {callback_url}")
    print("Follow browser prompts, then paste full redirected URL when requested.")

    oauth_attempts = int(os.environ.get("SCHWAB_OAUTH_ATTEMPTS", "2"))
    oauth_retry_sec = float(os.environ.get("SCHWAB_OAUTH_RETRY_SEC", "2"))
    try:
        client = _create_client_with_retries(
            schwab_module=schwab,
            api_key=api_key,
            app_secret=app_secret,
            callback_url=callback_url,
            token_path=token_path,
            attempts=max(1, oauth_attempts),
            retry_sec=max(0.5, oauth_retry_sec),
        )
    except Exception as exc:
        if _is_timeout_error(exc):
            sys.exit(
                "Schwab OAuth timed out after retries. "
                "Check network stability and confirm your app's callback URL "
                f"matches exactly: {callback_url}"
            )
        sys.exit(f"Failed to initialize Schwab OAuth flow: {exc}")

    verify_attempts = int(os.environ.get("SCHWAB_VERIFY_ATTEMPTS", "3"))
    verify_retry_sec = float(os.environ.get("SCHWAB_VERIFY_RETRY_SEC", "2"))
    _verify_token_with_retries(
        client,
        attempts=max(1, verify_attempts),
        retry_sec=max(0.5, verify_retry_sec),
    )


if __name__ == "__main__":
    main()
