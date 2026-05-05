import logging
import os
import re
import html

import requests
import streamlit as st
from dotenv import dotenv_values, load_dotenv, set_key

from config.settings import Settings
from core.pr_processor import PRProcessor

ENV_FILE = ".env"
APP_TITLE = "PR Auto-Reviewer"

ENV_SECTIONS = {
    "Required for Scan": [
        {
            "key": "GITHUB_TOKEN",
            "label": "GitHub Token",
            "secret": True,
            "help": "1) Open https://github.com/settings/tokens\n2) Create token with repo and pull_requests\n3) Paste the token value here",
        },
        {
            "key": "GITHUB_REVIEWER_LOGIN",
            "label": "GitHub Reviewer Login",
            "secret": False,
            "help": "1) Open your GitHub profile\n2) Copy your username/login\n3) Use the same login that owns the token",
        },
    ],
    "GitHub Copilot": [
        {
            "key": "COPILOT_MODEL",
            "label": "Copilot Model",
            "secret": False,
            "help": "Modelo a usar na GitHub Copilot API. Exemplos: gpt-4o, gpt-4o-mini, claude-3.5-sonnet.",
        },
    ],
}


class StreamlitLogHandler(logging.Handler):
    def __init__(self, on_message):
        super().__init__(level=logging.INFO)
        self.on_message = on_message

    def emit(self, record):
        self.on_message(self.format(record))


def _streamlit_secrets() -> dict:
    """Return st.secrets as a plain dict, or empty dict if not available."""
    try:
        return {k: str(v) for k, v in st.secrets.items()}
    except Exception:
        return {}


def _running_on_cloud() -> bool:
    """True when there is no local .env file (i.e. deployed on Streamlit Cloud)."""
    return not os.path.exists(ENV_FILE)


def load_env_map() -> dict:
    # On Streamlit Cloud there is no .env — use st.secrets instead.
    if _running_on_cloud():
        return _streamlit_secrets()
    values = dotenv_values(ENV_FILE)
    return {k: (v or "") for k, v in values.items()}


def save_env_map(values: dict):
    if _running_on_cloud():
        # Secrets on Streamlit Cloud are read-only at runtime.
        # Just push to os.environ for the current session.
        for key, value in values.items():
            os.environ[key] = value
        return
    for key, value in values.items():
        set_key(ENV_FILE, key, value, quote_mode="never")
        os.environ[key] = value
    load_dotenv(override=True)


def ensure_state():
    if "env_values" not in st.session_state:
        env_values = load_env_map()
        for section_fields in ENV_SECTIONS.values():
            for spec in section_fields:
                env_values.setdefault(spec["key"], "")
        st.session_state.env_values = env_values

    if "pr_urls" not in st.session_state:
        raw = st.session_state.env_values.get("GITHUB_PR_URL", "")
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        st.session_state.pr_urls = urls if urls else [""]

    if "analysis_logs" not in st.session_state:
        st.session_state.analysis_logs = []

    if "pr_statuses" not in st.session_state:
        st.session_state.pr_statuses = {}


_GITHUB_PR_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+/pull/\d+/?(?:[?#].*)?$"
)


def _validate_pr_url(url: str) -> str | None:
    """Return None if valid, or an error message string if invalid."""
    if not _GITHUB_PR_RE.match(url):
        return (
            f"Invalid PR URL: '{url}'. Must match "
            "https://github.com/<org>/<repo>/pull/<number>"
        )
    return None


def _get_invalid_url_messages(urls: list[str]) -> list[str]:
    return [msg for url in urls if (msg := _validate_pr_url(url))]


def _format_user_error(exc: Exception) -> str:
    """Map technical exceptions to actionable user-facing messages."""
    github_error = _format_github_http_error(exc)
    if github_error:
        return github_error

    return str(exc)


def _format_github_http_error(exc: Exception) -> str | None:
    if not isinstance(exc, requests.HTTPError):
        return None

    response = exc.response
    status = response.status_code if response is not None else None
    host = ""
    if response is not None and response.request is not None:
        host = response.request.url or ""

    if "api.github.com" not in host:
        return None

    if status in (401, 403):
        return (
            "GitHub authentication/permissions failed. "
            "Check GITHUB_TOKEN and ensure it has repo/pull_requests access."
        )
    if status == 404:
        return "PR not found or you do not have access to this repository."
    if status == 429:
        return "GitHub API rate limit reached. Please try again in a few minutes."
    return f"GitHub API error (HTTP {status})."


def render_settings_tab():
    st.subheader("Settings")
    if _running_on_cloud():
        st.info(
            "Running on Streamlit Cloud. Secrets are managed in the Streamlit Cloud dashboard "
            "(App settings → Secrets). Changes made here only last for the current session.",
            icon="☁️",
        )
    else:
        st.caption("Only variables required for the scan to run are shown here.")

    for section_name, fields in ENV_SECTIONS.items():
        with st.expander(section_name, expanded=True):
            for spec in fields:
                key = spec["key"]
                label = f"{spec['label']} ({key})"
                if spec["secret"]:
                    value = st.text_input(
                        label,
                        value=st.session_state.env_values.get(key, ""),
                        type="password",
                        help=spec["help"],
                        key=f"input_{key}",
                    )
                else:
                    value = st.text_input(
                        label,
                        value=st.session_state.env_values.get(key, ""),
                        help=spec["help"],
                        key=f"input_{key}",
                    )
                st.session_state.env_values[key] = value.strip()

    col_save, col_reload, _ = st.columns([2, 2, 4], gap="small")
    with col_save:
        if st.button("Save Settings", use_container_width=True):
            save_env_map(st.session_state.env_values)
            st.success("Settings saved to .env")
    with col_reload:
        if st.button("Reload from .env", use_container_width=True):
            st.session_state.env_values = load_env_map()
            st.success("Settings reloaded")
            st.rerun()


def build_settings_for_run(pr_urls: list[str]) -> Settings:
    for k, v in st.session_state.env_values.items():
        os.environ[k] = v

    joined = ",".join(pr_urls)
    os.environ["GITHUB_PR_URL"] = joined
    st.session_state.env_values["GITHUB_PR_URL"] = joined

    settings = Settings()
    settings.github_pr_url = joined
    return settings


def _get_pr_status(processor: PRProcessor, pr_url: str) -> tuple[str, str]:
    """Return PR lifecycle status and title.

    Status values:
      - open
      - merged
      - closed
    """
    owner, repo, pr_number = processor.github.parse_pr_url(pr_url)
    pr_info = processor.github.get_pr_info(owner, repo, pr_number)
    title = pr_info.get("title", f"PR #{pr_number}")
    is_merged = bool(pr_info.get("merged")) or bool(pr_info.get("merged_at"))
    state = (pr_info.get("state") or "").lower()

    if is_merged:
        return "merged", title
    if state == "open":
        return "open", title
    return "closed", title


def _clear_pr_url(index: int):
    """Clear a single PR URL input safely via callback."""
    if index < len(st.session_state.pr_urls):
        st.session_state.pr_urls[index] = ""
    key = f"pr_url_{index}"
    if key in st.session_state:
        st.session_state[key] = ""


def _render_pr_url_inputs():
    for i, _ in enumerate(st.session_state.pr_urls):
        col_input, col_clear = st.columns([22, 1], gap="small")
        with col_input:
            st.session_state.pr_urls[i] = st.text_input(
                f"PR URL #{i + 1}",
                value=st.session_state.pr_urls[i],
                key=f"pr_url_{i}",
                placeholder="https://github.com/org/repo/pull/123",
            )
        with col_clear:
            st.button(
                "✕",
                key=f"clear_pr_url_{i}",
                help="Clear URL",
                type="secondary",
                use_container_width=True,
                on_click=_clear_pr_url,
                args=(i,),
            )


def _render_pr_url_controls():
    col_add, col_remove, _ = st.columns([2, 2, 4], gap="small")
    with col_add:
        if st.button("Add URL Line", use_container_width=True):
            st.session_state.pr_urls.append("")
            st.rerun()
    with col_remove:
        if st.button("Remove Last Line", use_container_width=True) and len(st.session_state.pr_urls) > 1:
            st.session_state.pr_urls.pop()
            st.rerun()


def _get_validated_urls() -> list[str] | None:
    urls = [u.strip() for u in st.session_state.pr_urls if u.strip()]
    if not urls:
        st.error("Please add at least one valid PR URL.")
        return None

    errors = _get_invalid_url_messages(urls)
    if errors:
        for error in errors:
            st.error(error)
        return None

    return urls


def _run_analysis(urls: list[str], push_log) -> tuple[int, int]:
    settings = build_settings_for_run(urls)
    settings.validate()

    processor = PRProcessor(settings)
    processed_count = 0
    skipped_non_open_count = 0

    for url in urls:
        pr_state, pr_title = _get_pr_status(processor, url)
        if pr_state != "open":
            skipped_non_open_count += 1
            skip_msg = f"Skipping {pr_state} PR: {pr_title} ({url})"
            push_log(skip_msg)
            st.info(skip_msg)
            continue

        push_log(f"Processing: {url}")
        processor.process(url)
        processed_count += 1

    return processed_count, skipped_non_open_count


def _check_pr_statuses(urls: list[str]) -> dict[str, dict]:
    settings = build_settings_for_run(urls)
    if not settings.github_token:
        raise ValueError("GITHUB_TOKEN is required to check PR status.")

    processor = PRProcessor(settings)
    statuses: dict[str, dict] = {}

    for url in urls:
        try:
            pr_state, pr_title = _get_pr_status(processor, url)
            statuses[url] = {
                "title": pr_title,
                "state": pr_state,
            }
        except Exception as exc:
            statuses[url] = {
                "title": url,
                "state": "error",
                "error": _format_user_error(exc),
            }

    return statuses


def _render_pr_statuses():
    statuses = st.session_state.pr_statuses
    if not statuses:
        return

    st.caption("PR status preview")
    for url, details in statuses.items():
        title = html.escape(details.get("title", url))
        safe_url = html.escape(url)
        state = details.get("state")
        if state == "merged":
            st.markdown(
                (
                    "<div style='border:1px solid #8250df;background:#f5f0ff;color:#4c2889;"
                    "padding:8px 10px;border-radius:8px;margin-bottom:6px;'>"
                    "<strong style='margin-right:8px;'>MERGED</strong>"
                    f"{title} | {safe_url}"
                    "</div>"
                ),
                unsafe_allow_html=True,
            )
        elif state == "open":
            st.markdown(
                (
                    "<div style='border:1px solid #1a7f37;background:#dafbe1;color:#116329;"
                    "padding:8px 10px;border-radius:8px;margin-bottom:6px;'>"
                    "<strong style='margin-right:8px;'>OPEN</strong>"
                    f"{title} | {safe_url}"
                    "</div>"
                ),
                unsafe_allow_html=True,
            )
        elif state == "closed":
            st.markdown(
                (
                    "<div style='border:1px solid #9a6700;background:#fff8c5;color:#7d4e00;"
                    "padding:8px 10px;border-radius:8px;margin-bottom:6px;'>"
                    "<strong style='margin-right:8px;'>CLOSED</strong>"
                    f"{title} | {safe_url}"
                    "</div>"
                ),
                unsafe_allow_html=True,
            )
        else:
            error = html.escape(details.get("error", "Unknown error"))
            st.markdown(
                (
                    "<div style='border:1px solid #cf222e;background:#ffebe9;color:#a40e26;"
                    "padding:8px 10px;border-radius:8px;margin-bottom:6px;'>"
                    "<strong style='margin-right:8px;'>ERROR</strong>"
                    f"{title} | {safe_url} | {error}"
                    "</div>"
                ),
                unsafe_allow_html=True,
            )


def _auto_refresh_pr_statuses():
    """Auto-check status for valid PR URLs currently present in the input fields."""
    current_urls = [u.strip() for u in st.session_state.pr_urls if u.strip()]
    valid_urls = [u for u in current_urls if _validate_pr_url(u) is None]

    if not valid_urls:
        st.session_state.pr_statuses = {}
        return

    valid_url_set = set(valid_urls)
    st.session_state.pr_statuses = {
        url: status
        for url, status in st.session_state.pr_statuses.items()
        if url in valid_url_set
    }

    missing_urls = [url for url in valid_urls if url not in st.session_state.pr_statuses]
    if not missing_urls:
        return

    try:
        new_statuses = _check_pr_statuses(missing_urls)
    except Exception as exc:
        err = _format_user_error(exc)
        new_statuses = {
            url: {
                "title": url,
                "state": "error",
                "error": err,
            }
            for url in missing_urls
        }
    st.session_state.pr_statuses.update(new_statuses)


def _get_analyse_block_reason() -> str | None:
    current_urls = [u.strip() for u in st.session_state.pr_urls if u.strip()]
    if not current_urls:
        return "Add at least one PR URL to enable Analyse."

    invalid_errors = _get_invalid_url_messages(current_urls)
    if invalid_errors:
        return invalid_errors[0]

    statuses = st.session_state.pr_statuses
    if any(statuses.get(url, {}).get("state") == "merged" for url in current_urls):
        return "One or more PRs are already merged. Remove them to enable Analyse."

    if any(statuses.get(url, {}).get("state") == "closed" for url in current_urls):
        return "One or more PRs are closed (not merged). Remove them to enable Analyse."

    if any(statuses.get(url, {}).get("state") == "error" for url in current_urls):
        return "One or more PRs have status errors. Fix them to enable Analyse."

    if any(url not in statuses for url in current_urls):
        return "Waiting for PR status validation..."

    return None


def _render_analysis_summary(processed_count: int, skipped_merged_count: int, push_log):
    if processed_count == 0:
        push_log("No analysis generated: all provided PRs are already merged.")
        st.warning("All provided PRs are already merged. No analysis was generated.")
        return

    push_log("Analysis finished successfully.")
    success_msg = f"Review process completed for {processed_count} PR(s)."
    if skipped_merged_count:
        success_msg += f" Skipped {skipped_merged_count} merged PR(s)."
    st.success(success_msg)


def render_analyse_tab():
    st.subheader("Analyse PRs")
    st.caption("Add one PR URL per line and click Analyse.")

    _render_pr_url_inputs()
    _render_pr_url_controls()

    current_urls = [u.strip() for u in st.session_state.pr_urls if u.strip()]
    for error in _get_invalid_url_messages(current_urls):
        st.error(error)

    _auto_refresh_pr_statuses()

    _render_pr_statuses()

    analyse_block_reason = _get_analyse_block_reason()

    logs_box = st.empty()

    def push_log(message: str):
        st.session_state.analysis_logs.append(message)
        lines = st.session_state.analysis_logs[-250:]
        logs_box.code("\n".join(lines), language="text")

    if st.button(
        "Analyse",
        type="primary",
        disabled=bool(analyse_block_reason),
        help=analyse_block_reason or "Run automated review for all valid OPEN PR URLs.",
    ):
        urls = _get_validated_urls()
        if urls is None:
            return

        st.session_state.analysis_logs = []
        push_log("Starting analysis...")

        log_handler = StreamlitLogHandler(push_log)
        log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))

        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)
        root_logger.setLevel(logging.INFO)

        try:
            processed_count, skipped_merged_count = _run_analysis(urls, push_log)
            _render_analysis_summary(processed_count, skipped_merged_count, push_log)
        except Exception as exc:
            friendly_error = _format_user_error(exc)
            push_log(f"ERROR: {friendly_error}")
            st.error(f"Analysis failed: {friendly_error}")
        finally:
            root_logger.removeHandler(log_handler)


st.set_page_config(page_title=APP_TITLE, page_icon="🤖", layout="centered")
st.markdown(
    """
<style>
section.main > div.block-container {
    padding-top: 2rem;
    padding-bottom: 2rem;
}

.stButton > button {
    width: auto;
    min-height: 2rem;
    padding: 0.3rem 0.75rem;
    font-size: 0.88rem;
    border-radius: 0.45rem;
}

div[class*="st-key-clear_pr_url_"] button {
    margin-top: 1.72rem;
    width: 2.2rem;
    min-width: 2.2rem;
    height: 2.2rem;
    min-height: 2.2rem;
    padding: 0;
    border-radius: 999px;
    border: 1px solid #30363d;
    background: transparent;
    color: #8b949e;
    font-size: 0.95rem;
    line-height: 1;
    box-shadow: none;
}

div[class*="st-key-clear_pr_url_"] button:hover {
    border-color: #484f58;
    background: #161b22;
    color: #c9d1d9;
}

@media (max-width: 640px) {
    section.main > div.block-container {
        padding-left: 0.75rem;
        padding-right: 0.75rem;
    }
}
</style>
""",
    unsafe_allow_html=True,
)
st.title("🤖 " + APP_TITLE)
st.caption("Analyse GitHub Pull Requests automatically using GitHub Models (Copilot/EDP).")

ensure_state()

if _running_on_cloud():
    render_analyse_tab()
else:
    tab_analyse, tab_settings = st.tabs(["Analyse", "Settings"])

    with tab_analyse:
        render_analyse_tab()

    with tab_settings:
        render_settings_tab()
