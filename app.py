import logging
import os
import re

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
    "Anthropic": [
        {
            "key": "ANTHROPIC_API_KEY",
            "label": "Anthropic API Key",
            "secret": True,
            "help": "1) Open https://console.anthropic.com/keys\n2) Generate a new API key\n3) Paste it here",
        },
        {
            "key": "CLAUDE_MODEL",
            "label": "Claude Model",
            "secret": False,
            "help": "Use a model available in your account, for example claude-sonnet-4-5-20250929",
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

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False


_GITHUB_PR_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+/pull/\d+$"
)


def _validate_pr_url(url: str) -> str | None:
    """Return None if valid, or an error message string if invalid."""
    if not _GITHUB_PR_RE.match(url):
        return f"Invalid PR URL: '{url}'. Must match https://github.com/<org>/<repo>/pull/<number>"
    return None


def _get_app_password() -> str | None:
    """Return the configured APP_PASSWORD, or None if not set (open access)."""
    try:
        pwd = st.secrets.get("APP_PASSWORD", "")
        return pwd if pwd else None
    except Exception:
        return None


def render_login_gate():
    """Render a password prompt and block the rest of the app until authenticated."""
    required = _get_app_password()
    if required is None or st.session_state.authenticated:
        return True  # no password configured, or already authenticated

    st.title(APP_TITLE)
    st.subheader("Access")
    pwd = st.text_input("Password", type="password", key="login_pwd")
    if st.button("Enter"):
        if pwd == required:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


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

    col_save, col_reload, _ = st.columns([1, 1, 6], gap="small")
    with col_save:
        if st.button("Save Settings"):
            save_env_map(st.session_state.env_values)
            st.success("Settings saved to .env")
    with col_reload:
        if st.button("Reload from .env"):
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


def render_analyse_tab():
    st.subheader("Analyse PRs")
    st.caption("Add one PR URL per line and click Analyse.")

    for i, _ in enumerate(st.session_state.pr_urls):
        st.session_state.pr_urls[i] = st.text_input(
            f"PR URL #{i + 1}",
            value=st.session_state.pr_urls[i],
            key=f"pr_url_{i}",
            placeholder="https://github.com/org/repo/pull/123",
        )

    col_add, col_remove, _ = st.columns([1, 1, 6], gap="small")
    with col_add:
        if st.button("Add URL Line"):
            st.session_state.pr_urls.append("")
            st.rerun()
    with col_remove:
        if st.button("Remove Last Line") and len(st.session_state.pr_urls) > 1:
            st.session_state.pr_urls.pop()
            st.rerun()

    logs_box = st.empty()

    def push_log(message: str):
        st.session_state.analysis_logs.append(message)
        lines = st.session_state.analysis_logs[-250:]
        logs_box.code("\n".join(lines), language="text")

    if st.button("Analyse", type="primary"):
        urls = [u.strip() for u in st.session_state.pr_urls if u.strip()]
        if not urls:
            st.error("Please add at least one valid PR URL.")
            return

        errors = [msg for url in urls if (msg := _validate_pr_url(url))]
        if errors:
            for e in errors:
                st.error(e)
            return

        st.session_state.analysis_logs = []
        push_log("Starting analysis...")

        log_handler = StreamlitLogHandler(push_log)
        log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))

        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)
        root_logger.setLevel(logging.INFO)

        try:
            settings = build_settings_for_run(urls)
            settings.validate()

            processor = PRProcessor(settings)

            for url in urls:
                push_log(f"Processing: {url}")
                processor.process(url)

            push_log("Analysis finished successfully.")
            st.success("Review process completed.")
        except Exception as exc:
            push_log(f"ERROR: {exc}")
            st.error(f"Analysis failed: {exc}")
        finally:
            root_logger.removeHandler(log_handler)


st.set_page_config(page_title=APP_TITLE, page_icon="PR", layout="wide")
st.markdown(
    """
<style>
/* Keep app content centered and readable on large screens */
section.main > div.block-container {
    max-width: 920px;
    margin: 0 auto;
    padding-top: 1.25rem;
    padding-bottom: 1.25rem;
}

/* Smaller buttons with better wrapping behavior */
.stButton > button {
    width: auto;
    min-height: 2rem;
    padding: 0.3rem 0.75rem;
    font-size: 0.88rem;
    border-radius: 0.45rem;
}

/* Compact spacing for narrow screens */
@media (max-width: 768px) {
    section.main > div.block-container {
        max-width: 100%;
        padding-left: 0.8rem;
        padding-right: 0.8rem;
    }

    .stButton > button {
        min-height: 1.9rem;
        padding: 0.25rem 0.65rem;
        font-size: 0.84rem;
    }
}
</style>
""",
    unsafe_allow_html=True,
)
st.title(APP_TITLE)
st.caption("Simple UI to configure .env and run PR analysis with live step-by-step logs.")

ensure_state()

if not render_login_gate():
    st.stop()

if _running_on_cloud():
    render_analyse_tab()
else:
    tab_analyse, tab_settings = st.tabs(["Analyse", "Settings"])

    with tab_analyse:
        render_analyse_tab()

    with tab_settings:
        render_settings_tab()
