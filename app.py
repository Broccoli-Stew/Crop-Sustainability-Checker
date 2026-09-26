import html
import io
import json
import os
import textwrap
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
import folium
from folium.plugins import LocateControl
from branca.element import MacroElement
from streamlit_folium import st_folium
from PIL import Image
from jinja2 import Template
import qrcode

from crop_data import (
    CROP_THRESHOLDS,
)
from data_sources import fetch_climate, fetch_soil, fetch_terrain
from suitability import evaluate, get_factor_scores
from disease_model import load_model, predict, supported_plant_names, supports_plant
from croppy_agent import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_URL,
    ask_croppy,
    build_knowledge_context,
    build_system_prompt,
)
from localization import LANGUAGES, t as tr
from local_store import (
    authenticate,
    create_user,
    get_history,
    get_location_cache,
    init_db,
    put_location_cache,
    record_history,
)
from plant_health import first_steps
from polyculture import recommendations


# ============================================================
# PAGE CONFIG
# ============================================================

APP_DIR = Path(__file__).resolve().parent
LOGO_PATH = APP_DIR / "assets" / "terrasense-logo.png"
FAVICON_PATH = APP_DIR / "assets" / "terrasense-favicon.png"
if FAVICON_PATH.is_file():
    with Image.open(FAVICON_PATH) as favicon_file:
        FAVICON_IMAGE = favicon_file.convert("RGBA")
elif LOGO_PATH.is_file():
    with Image.open(LOGO_PATH) as favicon_file:
        FAVICON_IMAGE = favicon_file.convert("RGBA")
else:
    FAVICON_IMAGE = ":material/eco:"

st.set_page_config(
    page_title="Terrasense | Field planning companion",
    page_icon=FAVICON_IMAGE,
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# CONSTANTS
# ============================================================

DEFAULT_LAT = 25.2854
DEFAULT_LON = 51.5310

PAGES = ["map", "planner", "doctor", "history", "impact", "share"]


# ============================================================
# SESSION STATE
# ============================================================

if "page" not in st.session_state:
    st.session_state.page = PAGES[0]

if "page_navigation" not in st.session_state:
    st.session_state.page_navigation = st.session_state.page

if "theme_mode" not in st.session_state:
    st.session_state.theme_mode = "Light"

if "language" not in st.session_state:
    st.session_state.language = "en"

if "offline_mode" not in st.session_state:
    st.session_state.offline_mode = False

if "username" not in st.session_state:
    st.session_state.username = None

if "lat" not in st.session_state:
    st.session_state.lat = DEFAULT_LAT

if "lon" not in st.session_state:
    st.session_state.lon = DEFAULT_LON

if "field_point_selected" not in st.session_state:
    st.session_state.field_point_selected = False

if "field_map_generation" not in st.session_state:
    st.session_state.field_map_generation = 0

if "location_auto_start" not in st.session_state:
    st.session_state.location_auto_start = True

if "planner_lat" not in st.session_state:
    st.session_state.planner_lat = st.session_state.lat

if "planner_lon" not in st.session_state:
    st.session_state.planner_lon = st.session_state.lon

if "planner_point_selected" not in st.session_state:
    st.session_state.planner_point_selected = False

if "planner_point_source" not in st.session_state:
    st.session_state.planner_point_source = None

if "planner_map_generation" not in st.session_state:
    st.session_state.planner_map_generation = 0

if "planner_last_map_click" not in st.session_state:
    st.session_state.planner_last_map_click = None

if "analysis" not in st.session_state:
    st.session_state.analysis = None

if "crop_results" not in st.session_state:
    st.session_state.crop_results = None

if "disease_results" not in st.session_state:
    st.session_state.disease_results = None

if "croppy_open" not in st.session_state:
    st.session_state.croppy_open = False

if "croppy_provider" not in st.session_state:
    st.session_state.croppy_provider = "cloud"

if "croppy_messages" not in st.session_state:
    st.session_state.croppy_messages = []

if "croppy_last_error" not in st.session_state:
    st.session_state.croppy_last_error = None

if "last_map_click" not in st.session_state:
    st.session_state.last_map_click = None

if "latitude_input" not in st.session_state:
    st.session_state.latitude_input = DEFAULT_LAT

if "longitude_input" not in st.session_state:
    st.session_state.longitude_input = DEFAULT_LON

if "manual_field_data" not in st.session_state:
    st.session_state.manual_field_data = False

try:
    init_db()
    DB_READY = True
except Exception:
    DB_READY = False


# ============================================================
# HELPERS
# ============================================================

def safe_text(value):
    """Safely display text inside HTML."""
    if value is None:
        return "—"
    return html.escape(str(value))


def render_html(markup):
    """Render an HTML fragment without Markdown interpreting indentation as code."""
    st.markdown(textwrap.dedent(markup).strip(), unsafe_allow_html=True)


class LocationClickBridge(MacroElement):
    """Forward a browser geolocation result as a normal map click for Streamlit."""

    _template = Template(
        """
        {% macro script(this, kwargs) %}
        var terrasenseMap = {{ this._parent.get_name() }};
        terrasenseMap.on('locationfound', function(event) {
            terrasenseMap.fire('click', {latlng: event.latlng});
        });
        {% endmacro %}
        """
    )


def add_location_controls(map_object, auto_start=False):
    LocateControl(
        auto_start=auto_start,
        position="topleft",
        strings={"title": "Show my location"},
        flyTo=True,
        keepCurrentZoomLevel=False,
        showPopup=True,
    ).add_to(map_object)
    LocationClickBridge().add_to(map_object)


def get_location_coordinates(map_data):
    clicked = map_data.get("last_clicked") if isinstance(map_data, dict) else None
    if not clicked:
        return None
    try:
        latitude = round(float(clicked["lat"]), 5)
        longitude = round(float(clicked["lng"]), 5)
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def score_percent(score):
    if score is None:
        return 0

    try:
        return int(round(float(score) * 100))
    except (TypeError, ValueError):
        return 0


def factor_status(score):
    if score is None:
        return "Unavailable"

    if score >= 0.8:
        return "Good"

    if score >= 0.5:
        return "Moderate"

    return "Poor"


def field_result_summary(crop, verdict, score, factors=None, language="en"):
    """Turn the screening result into a short, farmer-facing explanation."""
    if verdict == "Unknown":
        return tr("field_unknown", language).format(crop=crop)

    template_key = {
        "Suitable": "field_suitable",
        "Marginal": "field_marginal",
        "Not suitable": "field_unsuitable",
    }.get(verdict, "field_unknown")
    return tr(template_key, language).format(crop=crop, score=score_percent(score))


def leaf_result_summary(plant, disease, match_score, crop_probability, language="en"):
    """Describe crop-ranked and whole-model support without presenting a diagnosis."""
    return tr("disease_result", language).format(
        plant=plant,
        disease=disease,
        score=model_probability_label(match_score),
        crop_support=model_probability_label(crop_probability),
    )


def logo_svg(width):
    """Render a compact Terrasense mark if the uploaded image asset is missing."""
    return f'''<svg role="img" aria-label="Terrasense logo" width="{width}" height="{width}" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
      <defs><linearGradient id="terra" x1="0" y1="1" x2="1" y2="0"><stop stop-color="#10b981"/><stop offset="1" stop-color="#20c4d1"/></linearGradient></defs>
      <rect x="72" y="72" width="368" height="368" rx="112" fill="url(#terra)"/>
      <path d="M174 171c44 0 73 14 82 47 9-33 38-47 82-47 6 0 10 5 9 11-8 42-36 61-83 61h-2v103c0 8-5 13-13 13h-1c-8 0-13-5-13-13V243h-2c-47 0-75-19-83-61-1-6 3-11 9-11z" fill="#111827"/>
    </svg>'''


def render_logo(width):
    """Show the supplied PNG, with an inline vector fallback for root-only uploads."""
    if LOGO_PATH.is_file():
        st.image(str(LOGO_PATH), width=width)
    else:
        render_html(logo_svg(width))


def voice_field_summary(analysis):
    crop = analysis.get("crop", "selected crop")
    verdict = analysis.get("verdict", "Unknown")
    score = analysis.get("score")
    climate = analysis.get("climate", {})
    soil = analysis.get("soil", {})
    language = st.session_state.language
    summary = field_result_summary(crop, verdict, score, analysis.get("factors"), language)
    details = []
    for key in ("temp_c", "rain_mm_year"):
        value = climate.get(key)
        if value is not None:
            if key == "temp_c":
                details.append(
                    f"{tr('temperature', language)} {float(value):.1f} "
                    f"{tr('degrees_celsius', language)}"
                )
            else:
                details.append(
                    f"{tr('rainfall', language)} {float(value):.0f} "
                    f"{tr('millimeters_per_year', language)}"
                )
    if soil.get("ph") is not None:
        details.append(f"{tr('soil_ph', language)} {float(soil['ph']):.2f}")
    if soil.get("elevation_m") is not None:
        details.append(
            f"{tr('elevation', language)} {float(soil['elevation_m']):.0f} "
            f"{tr('meters', language)}"
        )
    if soil.get("slope_pct") is not None:
        details.append(
            f"{tr('slope', language)} {float(soil['slope_pct']):.1f} "
            f"{tr('percent', language)}"
        )
    return ". ".join([summary, *details, tr("screening_warning", language)])


def format_factor_value(value, unit):
    if value is None:
        return "Unavailable"

    try:
        if unit == "°C":
            return f"{float(value):.1f} °C"

        if unit == "mm/year":
            return f"{float(value):,.0f} mm/year"

        if unit == "pH":
            return f"{float(value):.2f}"

    except (TypeError, ValueError):
        pass

    return str(value)


def reset_analysis():
    st.session_state.analysis = None
    st.session_state.crop_results = None
    st.session_state.disease_results = None


def clear_disease_results():
    st.session_state.disease_results = None


def sync_field_coordinates():
    latitude = float(st.session_state.latitude_input)
    longitude = float(st.session_state.longitude_input)
    coordinates = (round(latitude, 5), round(longitude, 5))
    st.session_state.lat = latitude
    st.session_state.lon = longitude
    st.session_state.last_map_click = coordinates
    st.session_state.field_point_selected = True
    st.session_state.location_auto_start = False
    st.session_state.field_map_generation += 1
    sync_planner_from_field(latitude, longitude)
    reset_analysis()


def sync_planner_from_field(latitude, longitude):
    """Keep the planting map centered on the latest field-map selection."""
    coordinates = (round(float(latitude), 5), round(float(longitude), 5))
    st.session_state.planner_lat = float(latitude)
    st.session_state.planner_lon = float(longitude)
    st.session_state.planner_lat_input = float(latitude)
    st.session_state.planner_lon_input = float(longitude)
    st.session_state.planner_last_map_click = coordinates
    st.session_state.planner_point_selected = True
    st.session_state.planner_point_source = "field"
    st.session_state.planner_map_generation += 1
    st.session_state.crop_results = None


def clear_field_linked_planner_point():
    """Clear the planting point only when it was inherited from the field map."""
    if st.session_state.get("planner_point_source") != "field":
        return
    st.session_state.planner_point_source = None
    st.session_state.planner_point_selected = False
    st.session_state.planner_lat = DEFAULT_LAT
    st.session_state.planner_lon = DEFAULT_LON
    st.session_state.planner_lat_input = DEFAULT_LAT
    st.session_state.planner_lon_input = DEFAULT_LON
    st.session_state.planner_last_map_click = None
    st.session_state.planner_map_generation += 1
    st.session_state.crop_results = None


def sync_planner_coordinates():
    latitude = float(st.session_state.planner_lat_input)
    longitude = float(st.session_state.planner_lon_input)
    st.session_state.planner_lat = latitude
    st.session_state.planner_lon = longitude
    st.session_state.planner_last_map_click = (round(latitude, 5), round(longitude, 5))
    st.session_state.planner_point_selected = True
    st.session_state.planner_point_source = "planner"
    st.session_state.planner_map_generation += 1
    st.session_state.crop_results = None


def fetch_field_data(latitude, longitude, offline=False, manual=None):
    """Read the exact saved point offline, otherwise use free public sources and cache it."""
    cached = get_location_cache(latitude, longitude) if DB_READY else None
    if offline:
        if cached:
            return cached["climate"], cached["soil"], "saved"
        if manual and manual.get("enabled"):
            return (
                {
                    "temp_c": manual.get("temp_c"),
                    "rain_mm_year": manual.get("rain_mm_year"),
                    "humidity_pct": None,
                    "error": "Entered locally by the user.",
                },
                {"ph": manual.get("ph"), "error": "Entered locally by the user."},
                "manual",
            )
        empty_climate = {
            "temp_c": None, "rain_mm_year": None, "humidity_pct": None,
            "error": "No saved values for this point. Enter local values below or connect to refresh.",
        }
        return empty_climate, {"ph": None, "error": "No saved soil value for this point."}, "empty"

    climate = fetch_climate(latitude, longitude)
    soil = fetch_soil(latitude, longitude)
    terrain = fetch_terrain(latitude, longitude)
    soil.update(terrain)
    if cached:
        for key in ("temp_c", "rain_mm_year", "humidity_pct"):
            if climate.get(key) is None:
                climate[key] = cached.get("climate", {}).get(key)
        if soil.get("ph") is None:
            soil["ph"] = cached.get("soil", {}).get("ph")
        for key in ("elevation_m", "slope_pct"):
            if soil.get(key) is None:
                soil[key] = cached.get("soil", {}).get(key)
    if DB_READY and (climate.get("temp_c") is not None or soil.get("ph") is not None or soil.get("elevation_m") is not None):
        put_location_cache(latitude, longitude, {"climate": climate, "soil": soil})
    return climate, soil, "live"


def save_activity(entry_type, *, latitude=None, longitude=None, crop=None, outcome=None, score=None, details=None):
    username = st.session_state.get("username")
    if username and DB_READY:
        record_history(
            username,
            entry_type,
            latitude=latitude,
            longitude=longitude,
            crop=crop,
            outcome=outcome,
            score=score,
            details=details,
        )


@st.cache_resource(show_spinner=False)
def cached_disease_model(offline):
    return load_model(local_files_only=offline)


def render_voice_button(message):
    locale = {
        "en": "en-US",
        "ar": "ar-SA",
        "zh": "zh-CN",
        "fr": "fr-FR",
        "ru": "ru-RU",
        "es": "es-ES",
    }.get(st.session_state.language, "en-US")
    safe_message = json.dumps(message, ensure_ascii=False).replace("</", "<\\/")
    labels = {
        "idle": tr("read_aloud", st.session_state.language),
        "playing": tr("stop_reading", st.session_state.language),
        "unavailable": tr("voice_unavailable", st.session_state.language),
    }
    safe_labels = json.dumps(labels, ensure_ascii=False).replace("</", "<\\/")
    components.html(
        f"""<button type="button" aria-label="{html.escape(labels['idle'])}" aria-pressed="false" style="
            background:#174d38;color:white;border:0;border-radius:8px;
            padding:10px 16px;font-size:15px;font-weight:600;cursor:pointer">
            {html.escape(labels['idle'])}
        </button>
        <script>
        const button = document.currentScript.previousElementSibling;
        const labels = {safe_labels};
        const locale = '{locale}';
        let activeUtterance = null;
        let isPlaying = false;
        function setPlaying(value) {{
          isPlaying = value;
          button.textContent = value ? labels.playing : labels.idle;
          button.setAttribute('aria-label', value ? labels.playing : labels.idle);
          button.setAttribute('aria-pressed', value ? 'true' : 'false');
        }}
        function findVoice(voices) {{
          const requested = locale.toLowerCase();
          const languageCode = requested.split('-')[0];
          return voices.find(voice => voice.lang.toLowerCase() === requested)
            || voices.find(voice => voice.lang.toLowerCase().split('-')[0] === languageCode);
        }}
        async function matchingVoice() {{
          const voices = window.speechSynthesis.getVoices();
          if (voices.length) return findVoice(voices);
          await new Promise(resolve => {{
            const timeout = window.setTimeout(resolve, 1200);
            window.speechSynthesis.addEventListener('voiceschanged', () => {{
              window.clearTimeout(timeout);
              resolve();
            }}, {{ once: true }});
          }});
          return findVoice(window.speechSynthesis.getVoices());
        }}
        button.addEventListener('click', async () => {{
          if (!('speechSynthesis' in window) || !('SpeechSynthesisUtterance' in window)) {{
            button.textContent = labels.unavailable;
            return;
          }}
          if (isPlaying) {{
            setPlaying(false);
            activeUtterance = null;
            window.speechSynthesis.cancel();
            return;
          }}
          window.speechSynthesis.cancel();
          const voice = await matchingVoice();
          if (!voice) {{
            button.textContent = labels.unavailable;
            button.setAttribute('aria-label', labels.unavailable);
            button.setAttribute('aria-pressed', 'false');
            window.setTimeout(() => setPlaying(false), 2500);
            return;
          }}
          const utterance = new SpeechSynthesisUtterance({safe_message});
          activeUtterance = utterance;
          utterance.voice = voice;
          utterance.lang = voice.lang || locale;
          utterance.onend = () => {{ if (activeUtterance === utterance) setPlaying(false); }};
          utterance.onerror = () => {{ if (activeUtterance === utterance) setPlaying(false); }};
          setPlaying(true);
          window.speechSynthesis.speak(utterance);
        }});
        </script>""",
        height=54,
    )


def render_copy_link_button(url):
    label = tr("copy_link", st.session_state.language)
    copied_label = tr("link_copied", st.session_state.language)
    failed_label = tr("copy_failed", st.session_state.language)
    safe_url = json.dumps(url, ensure_ascii=False).replace("</", "<\\/")
    labels = json.dumps(
        {"copied": copied_label, "failed": failed_label},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    components.html(
        f"""<button id="copy-app-link" type="button" style="
            background:#174d38;color:white;border:0;border-radius:8px;
            padding:10px 16px;font-size:15px;font-weight:600;cursor:pointer">
            {html.escape(label)}
        </button>
        <span id="copy-app-link-status" role="status" aria-live="polite"
            style="display:block;min-height:1.2em;margin-top:4px"></span>
        <script>
        const button = document.getElementById('copy-app-link');
        const status = document.getElementById('copy-app-link-status');
        const appUrl = {safe_url};
        const messages = {labels};
        async function copyAppLink() {{
          let copied = false;
          try {{
            await window.parent.navigator.clipboard.writeText(appUrl);
            copied = true;
          }} catch (error) {{
            try {{
              const field = document.createElement('textarea');
              field.value = appUrl;
              field.style.position = 'fixed';
              field.style.opacity = '0';
              document.body.appendChild(field);
              field.focus();
              field.select();
              copied = document.execCommand('copy');
              field.remove();
            }} catch (fallbackError) {{
              copied = false;
            }}
          }}
          status.textContent = copied ? messages.copied : messages.failed;
          if (copied) {{
            window.setTimeout(() => {{ status.textContent = ''; }}, 2500);
          }}
        }}
        button.addEventListener('click', copyAppLink);
        </script>""",
        height=72,
    )


def croppy_setting(name, default=""):
    """Read a provider setting from the process environment or Streamlit secrets."""
    environment_value = os.environ.get(name)
    if environment_value:
        return environment_value
    try:
        return str(st.secrets.get(name, default) or default)
    except Exception:
        return default


def model_probability_label(probability):
    if probability is None:
        return "Unavailable"
    percent = max(0.0, float(probability) * 100)
    return "<0.1%" if percent < 0.1 else f"{percent:.1f}%"


def croppy_screen_context():
    """Share only short text summaries of the current result, never uploaded images."""
    sections = []
    disease_results = st.session_state.get("disease_results") or []
    if st.session_state.get("page") == "doctor" and disease_results:
        result = disease_results[0]
        if result.get("unsupported_crop"):
            sections.append(
                f"The current leaf screen says the selected crop {result.get('plant')} "
                "is not supported by the image model."
            )
        else:
            support = model_probability_label(result.get("crop_probability"))
            class_score = model_probability_label(result.get("confidence"))
            sections.append(
                f"The current leaf screen's top crop-specific class is "
                f"{result.get('disease')} on {result.get('plant')}; its score within "
                f"that crop's labels is {class_score}, and overall model support for "
                f"the crop is {support}. This is not a confirmed diagnosis."
            )
    analysis = st.session_state.get("analysis")
    if analysis and st.session_state.get("page") == "map":
        sections.append(
            f"The current field screen is for {analysis.get('crop')} and its broad "
            f"suitability verdict is {analysis.get('verdict')}. No exact coordinates are included."
        )
    return " ".join(sections)


def croppy_reply(prompt, language):
    language_name = next(
        (name for name, code in LANGUAGES.items() if code == language), "English"
    )
    knowledge = build_knowledge_context(prompt, croppy_screen_context())
    system_prompt = build_system_prompt(language_name, knowledge)
    return ask_croppy(
        provider=st.session_state.croppy_provider,
        prompt=prompt,
        history=st.session_state.croppy_messages,
        system_prompt=system_prompt,
        api_key=croppy_setting("GEMINI_API_KEY"),
        gemini_model=croppy_setting("CROPPY_GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        ollama_url=croppy_setting("CROPPY_OLLAMA_URL", DEFAULT_OLLAMA_URL),
        ollama_model=croppy_setting("CROPPY_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
    )


# ============================================================
# CUSTOM CSS
# ============================================================

dark_theme = st.session_state.theme_mode == "Dark"
theme = {
    "page": "#0b0f14" if dark_theme else "#ffffff",
    "surface": "#151b22" if dark_theme else "#f7faf8",
    "card": "#1b232c" if dark_theme else "#ffffff",
    "text": "#edf2f7" if dark_theme else "#18231d",
    "muted": "#aab6c2" if dark_theme else "#5c6b62",
    "border": "#34404c" if dark_theme else "#dce5df",
    "accent": "#72d6b2" if dark_theme else "#176b4d",
    "hero": "#162720" if dark_theme else "#eef8f1",
}

render_html(
    f"""
    <style>
    :root {{
        color-scheme: {"dark" if dark_theme else "light"};
        --app-page: {theme["page"]};
        --app-surface: {theme["surface"]};
        --app-card: {theme["card"]};
        --app-text: {theme["text"]};
        --app-muted: {theme["muted"]};
        --app-border: {theme["border"]};
        --app-accent: {theme["accent"]};
        --app-hero: {theme["hero"]};
    }}
    html, body, .stApp, [data-testid="stAppViewContainer"],
    [data-testid="stMain"], [data-testid="stHeader"],
    [data-testid="stSidebar"], [data-testid="stBottom"] {{
        background-color: var(--app-page) !important;
        color: var(--app-text) !important;
    }}
    .stApp, [data-testid="stAppViewContainer"] {{ min-height: 100vh; }}
    [data-testid="stMain"] > div, [data-testid="stSidebar"] > div {{
        background-color: var(--app-page) !important;
    }}
    [data-testid="stSidebar"] {{ border-right: 1px solid var(--app-border); }}
    [data-testid="stHeader"] {{ border-bottom: 1px solid var(--app-border); }}
    [data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] li, [data-testid="stMarkdownContainer"] h1,
    [data-testid="stMarkdownContainer"] h2, [data-testid="stMarkdownContainer"] h3,
    [data-testid="stMarkdownContainer"] h4, [data-testid="stMarkdownContainer"] h5,
    [data-testid="stMarkdownContainer"] h6, [data-testid="stCaptionContainer"] {{
        color: var(--app-text) !important;
    }}
    [data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] *,
    [data-testid="stRadio"] label, [data-testid="stCheckbox"] label,
    [data-testid="stSelectbox"] label, [data-testid="stNumberInput"] label {{
        color: var(--app-text) !important;
    }}
    [data-testid="stCaptionContainer"] {{ opacity: 0.84; }}
    [data-testid="stMetric"], [data-testid="stVerticalBlockBorderWrapper"] {{
        background-color: var(--app-card) !important;
        border-color: var(--app-border) !important;
    }}
    [data-testid="stMetricLabel"], [data-testid="stMetricValue"] {{ color: var(--app-text) !important; }}
    input, textarea, [data-baseweb="select"] > div,
    [data-baseweb="input"] > div, [data-baseweb="textarea"] > div {{
        background-color: var(--app-card) !important;
        color: var(--app-text) !important;
        border-color: var(--app-border) !important;
    }}
    [data-testid="stExpander"] {{
        background-color: var(--app-card) !important;
        border-color: var(--app-border) !important;
    }}
    [data-testid="stAlert"] {{ background-color: var(--app-surface) !important; }}
    [data-testid="stAlert"] *, [data-testid="stExpander"] * {{ color: var(--app-text) !important; }}
    [data-testid="stBaseButton-secondary"] {{
        background-color: var(--app-surface) !important;
        color: var(--app-text) !important;
        border-color: var(--app-border) !important;
    }}
    [data-testid="stBaseButton-primary"] {{ background-color: var(--app-accent) !important; color: #ffffff !important; }}
    .st-key-croppy_launcher {{
        position: fixed; left: .75rem; bottom: .75rem;
        width: min(18rem, calc(100vw - 1.5rem)); z-index: 999998;
    }}
    .st-key-croppy_panel {{
        position: fixed !important; top: .75rem; bottom: .75rem; left: .75rem;
        width: min(390px, calc(100vw - 1.5rem)); overflow-y: auto;
        z-index: 999999; padding: 1rem; border: 1px solid var(--app-border);
        border-radius: 16px; background: var(--app-page) !important;
        box-shadow: 0 10px 36px rgba(0, 0, 0, .35);
    }}
    .block-container {{ padding-top: 1.5rem; padding-bottom: 2rem; max-width: 1400px; }}
    .hero {{
        padding: 1.3rem 1.5rem; border-radius: 18px;
        background: var(--app-hero); border: 1px solid var(--app-border);
        margin-bottom: 1rem; color: var(--app-text);
    }}
    .hero h1 {{ margin: 0; font-size: 2.25rem; font-weight: 800; color: var(--app-text) !important; }}
    .hero p {{ margin: .4rem 0 0; color: var(--app-muted) !important; font-size: 1.02rem; }}
    .feature-title {{ color: var(--app-accent); font-weight: 750; font-size: 1.05rem; margin-bottom: .35rem; }}
    footer {{ visibility: hidden; }}
    </style>
    """
)


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    language_names = list(LANGUAGES.keys())
    selected_language = st.selectbox(
        tr("language", st.session_state.language),
        language_names,
        index=list(LANGUAGES.values()).index(st.session_state.language),
        key="language_selector",
    )
    st.session_state.language = LANGUAGES[selected_language]
    language = st.session_state.language

    render_logo(104)
    st.markdown("## Terrasense")
    st.caption(tr("tagline", language))

    st.divider()

    st.selectbox("Appearance", ["Light", "Dark"], key="theme_mode")

    st.markdown(f"### {safe_text(tr('nav', language))}")

    for sidebar_page in PAGES:
        sidebar_label = tr(sidebar_page, language)

        if st.button(
            sidebar_label,
            use_container_width=True,
            type=(
                "primary"
                if st.session_state.page == sidebar_page
                else "secondary"
            ),
            key=f"sidebar_{sidebar_page}",
        ):

            if st.session_state.page != sidebar_page:
                st.session_state.page = sidebar_page
                st.session_state.page_navigation = sidebar_page
                st.rerun()

    st.divider()

    st.checkbox(
        tr("offline", language),
        key="offline_mode",
        help=tr("offline_note", language),
    )

    with st.expander(tr("account", language)):
        if st.session_state.username:
            st.success(f"Signed in as {safe_text(st.session_state.username)}")
            if st.button(tr("logout", language), use_container_width=True):
                st.session_state.username = None
                st.rerun()
        else:
            if not DB_READY:
                st.warning("Local account storage could not be opened on this installation.")
            else:
                login_tab, register_tab = st.tabs([tr("sign_in", language), tr("register", language)])
                with login_tab:
                    login_name = st.text_input(tr("username", language), key="login_username")
                    login_password = st.text_input(tr("password", language), type="password", key="login_password")
                    if st.button(tr("sign_in", language), key="sign_in_button", use_container_width=True):
                        if authenticate(login_name, login_password):
                            st.session_state.username = login_name.strip()
                            st.rerun()
                        st.error("The username or password was not recognized.")
                with register_tab:
                    new_name = st.text_input(tr("username", language), key="register_username")
                    new_password = st.text_input(tr("password", language), type="password", key="register_password")
                    if st.button(tr("register", language), key="register_button", use_container_width=True):
                        created, message = create_user(new_name, new_password)
                        (st.success if created else st.error)(message)
            st.caption("A local database stores a salted password hash and saved activity. Hosted retention depends on the deployment's storage.")

    st.caption(
        "Reboot the Earth 2026\n\n"
        "Challenge 1 • Team 17"
    )

# ============================================================
# HERO
# ============================================================

hero_logo, hero_copy = st.columns([0.12, 0.88], vertical_alignment="center")
with hero_logo:
    render_logo(68)
with hero_copy:
    render_html(
        f"""
        <div class="hero">
            <h1>Terrasense</h1>
            <p>{safe_text(tr("tagline", language))}<br>
            <span>{safe_text(tr("problem", language))}</span></p>
        </div>
        """
    )

feature_columns = st.columns(3)
feature_copy = [
    ("map", "map_intro"),
    ("planner", "planner_intro"),
    ("doctor", "doctor_intro"),
]
for column, (title_key, description_key) in zip(feature_columns, feature_copy):
    with column:
        st.markdown(f"#### {tr(title_key, language)}")
        st.caption(tr(description_key, language))


# ============================================================
# TOP NAVIGATION
# ============================================================

page = st.radio(
    tr("nav", language),
    PAGES,
    format_func=lambda page_key: tr(page_key, language),
    horizontal=True,
    label_visibility="collapsed",
    key="page_navigation",
)

if page != st.session_state.page:
    st.session_state.page = page


# ============================================================
# ANALYZE LAND
# ============================================================

if st.session_state.page == "map":

    st.subheader(tr("map_title", language))
    st.write(tr("map_intro", language))
    st.caption(tr("offline_note" if st.session_state.offline_mode else "online_note", language))

    col1, col2 = st.columns([2.1, 1])

    # ========================================================
    # MAP
    # ========================================================

    with col1:

        st.markdown("#### Select a location")

        field_map_location = (
            [st.session_state.lat, st.session_state.lon]
            if st.session_state.field_point_selected
            else [20.0, 0.0]
        )
        m = folium.Map(
            location=field_map_location,
            zoom_start=11 if st.session_state.field_point_selected else 2,
            tiles=None if st.session_state.offline_mode else "OpenStreetMap",
            control_scale=True,
        )
        add_location_controls(m, auto_start=st.session_state.location_auto_start)

        leaf_html = """
        <div style="
            position: relative;
            width: 48px;
            height: 58px;
            transform: translate(-12px, -50px);
        ">
            <div style="
                width: 42px;
                height: 42px;
                border-radius: 50%;
                background: white;
                border: 2px solid #43A047;
                box-shadow: 0 3px 10px rgba(0,0,0,0.25);
                display: flex;
                align-items: center;
                justify-content: center;
                position: absolute;
                top: 0;
                left: 0;
            ">
                <svg
                    width="25"
                    height="25"
                    viewBox="0 0 24 24"
                    fill="none"
                    xmlns="http://www.w3.org/2000/svg"
                >
                    <path
                        d="M20.7 3.3C14.1 3.5 8.8 5.1 5.7 8.2C2.8 11.1 3.1 15.7 4.2 18.1C6.6 19.2 11.2 19.5 14.1 16.6C17.2 13.5 18.8 8.2 20.7 3.3Z"
                        fill="#4CAF50"
                    />
                    <path
                        d="M4.5 19.5C7.2 15.6 10.4 12.5 15.5 9.5"
                        stroke="#1B5E20"
                        stroke-width="1.6"
                        stroke-linecap="round"
                    />
                </svg>
            </div>

            <div style="
                position: absolute;
                top: 38px;
                left: 17px;
                width: 0;
                height: 0;
                border-left: 6px solid transparent;
                border-right: 6px solid transparent;
                border-top: 10px solid #43A047;
            "></div>
        </div>
        """

        if st.session_state.field_point_selected:
            folium.Marker(
                [st.session_state.lat, st.session_state.lon],
                tooltip="Selected location",
                icon=folium.DivIcon(html=leaf_html),
            ).add_to(m)

        map_data = st_folium(
            m,
            height=470,
            width=None,
            returned_objects=["last_clicked"],
        key=f"terrasense_map_{st.session_state.field_map_generation}",
        )

        selected_coordinates = get_location_coordinates(map_data)
        if selected_coordinates and st.session_state.last_map_click != selected_coordinates:
            new_lat, new_lon = selected_coordinates
            st.session_state.last_map_click = selected_coordinates
            st.session_state.lat = new_lat
            st.session_state.lon = new_lon
            st.session_state.latitude_input = new_lat
            st.session_state.longitude_input = new_lon
            st.session_state.field_point_selected = True
            st.session_state.location_auto_start = False
            st.session_state.field_map_generation += 1
            sync_planner_from_field(new_lat, new_lon)
            reset_analysis()
            st.rerun()

        if st.session_state.field_point_selected:
            st.caption(f"Selected point: {st.session_state.lat:.5f}, {st.session_state.lon:.5f}")
        else:
            st.caption("The map will request your location. You can also zoom in and select any point.")

        if st.button(
            "Clear selected point",
            key="clear_field_point",
            use_container_width=True,
            disabled=not st.session_state.field_point_selected,
        ):
            st.session_state.field_point_selected = False
            st.session_state.location_auto_start = False
            st.session_state.field_map_generation += 1
            st.session_state.last_map_click = None
            st.session_state.lat = DEFAULT_LAT
            st.session_state.lon = DEFAULT_LON
            st.session_state.latitude_input = DEFAULT_LAT
            st.session_state.longitude_input = DEFAULT_LON
            clear_field_linked_planner_point()
            reset_analysis()
            st.rerun()

    # ========================================================
    # LOCATION CONTROLS
    # ========================================================

    with col2:

        st.markdown("#### Coordinates")

        latitude = st.number_input(
            "Latitude",
            min_value=-90.0,
            max_value=90.0,
            step=0.0001,
            format="%.5f",
            key="latitude_input",
            on_change=sync_field_coordinates,
        )

        longitude = st.number_input(
            "Longitude",
            min_value=-180.0,
            max_value=180.0,
            step=0.0001,
            format="%.5f",
            key="longitude_input",
            on_change=sync_field_coordinates,
        )

        st.session_state.lat = latitude
        st.session_state.lon = longitude

        st.caption(
            "Click anywhere on the map or enter coordinates manually. Browser location is requested on the field map and can be denied at any time."
        )

        with st.expander("Enter local climate and soil values", expanded=st.session_state.offline_mode):
            st.checkbox("Use values entered here when no saved point is available", key="manual_field_data")
            manual_temperature = st.number_input(
                tr("temperature", language), min_value=-40.0, max_value=60.0,
                value=None, step=0.5, key="manual_temperature",
            )
            manual_rainfall = st.number_input(
                tr("rainfall", language), min_value=0.0, max_value=20000.0,
                value=None, step=25.0, key="manual_rainfall",
            )
            manual_ph = st.number_input(
                tr("soil_ph", language), min_value=0.0, max_value=14.0,
                value=None, step=0.1, key="manual_ph",
            )

        reset = st.button(
            "Reset location",
            use_container_width=True,
        )

        if reset:

            st.session_state.lat = DEFAULT_LAT
            st.session_state.lon = DEFAULT_LON

            st.session_state.latitude_input = DEFAULT_LAT
            st.session_state.longitude_input = DEFAULT_LON

            st.session_state.last_map_click = None
            st.session_state.field_point_selected = False
            st.session_state.location_auto_start = False
            st.session_state.field_map_generation += 1
            clear_field_linked_planner_point()

            reset_analysis()

            st.rerun()

    # ========================================================
    # CROP SELECTION
    # ========================================================

    st.divider()

    st.subheader(tr("select_crop", language))

    crop_names = sorted(CROP_THRESHOLDS.keys())

    crop_options = [
        "Select a crop..."
    ] + crop_names

    selected_crop = st.selectbox(
        "Crop",
        crop_options,
        index=0,
    )

    # ========================================================
    # NO CROP SELECTED
    # ========================================================

    if selected_crop == "Select a crop...":

        st.info(
            "Select a crop to view its preferred conditions and analyze this location."
        )

    # ========================================================
    # CROP SELECTED
    # ========================================================

    else:

        thresholds = CROP_THRESHOLDS[selected_crop]

        with st.expander("View preferred conditions"):

            c1, c2, c3 = st.columns(3)

            with c1:
                st.metric(
                    "Temperature",
                    f"{thresholds['temp_c'][0]}–"
                    f"{thresholds['temp_c'][1]} °C",
                )

            with c2:
                st.metric(
                    "Rainfall",
                    f"{thresholds['rain_mm'][0]:,}–"
                    f"{thresholds['rain_mm'][1]:,} mm",
                )

            with c3:
                st.metric(
                    "Soil pH",
                    f"{thresholds['ph'][0]}–"
                    f"{thresholds['ph'][1]}",
                )

        st.caption(
            thresholds.get(
                "notes",
                "Preferred growing conditions.",
            )
        )

        # ====================================================
        # ANALYZE BUTTON
        # ====================================================

        if st.button(
            tr("analyze", language),
            type="primary",
            use_container_width=True,
            disabled=not st.session_state.field_point_selected,
        ):

            with st.spinner(
                "Fetching climate and soil data..."
            ):

                try:

                    manual = {
                        "enabled": st.session_state.get("manual_field_data", False),
                        "temp_c": st.session_state.get("manual_temperature"),
                        "rain_mm_year": st.session_state.get("manual_rainfall"),
                        "ph": st.session_state.get("manual_ph"),
                    }
                    climate, soil, data_mode = fetch_field_data(
                        st.session_state.lat,
                        st.session_state.lon,
                        offline=st.session_state.offline_mode,
                        manual=manual,
                    )

                    verdict, score, reasons = evaluate(
                        climate,
                        soil,
                        thresholds,
                    )

                    factors = get_factor_scores(
                        climate,
                        soil,
                        thresholds,
                    )

                    st.session_state.analysis = {
                        "crop": selected_crop,
                        "climate": climate,
                        "soil": soil,
                        "verdict": verdict,
                        "score": score,
                        "reasons": reasons,
                        "factors": factors,
                        "data_mode": data_mode,
                    }
                    save_activity(
                        "field suitability",
                        latitude=st.session_state.lat,
                        longitude=st.session_state.lon,
                        crop=selected_crop,
                        outcome=verdict,
                        score=score,
                        details={"data_mode": data_mode},
                    )

                except Exception:

                    st.error(
                        "We couldn't assess this location. Check the coordinates and "
                        "field values, then try again. If you are offline, use saved "
                        "data or enter local values."
                    )

    # ========================================================
    # RESULTS
    # ========================================================

    analysis = st.session_state.analysis

    if analysis:

        st.divider()

        st.subheader(
            f"Results for {analysis['crop']}"
        )

        verdict = analysis["verdict"]
        score = analysis["score"]

        with st.container(border=True):
            if verdict == "Suitable":
                st.success(verdict)
            elif verdict == "Not suitable":
                st.error(verdict)
            else:
                st.warning(verdict)
            st.metric("Suitability score", f"{score_percent(score)}%")

        st.subheader("Plain-language summary")
        st.write(
            field_result_summary(
                analysis["crop"],
                verdict,
                score,
                analysis.get("factors"),
                language=language,
            )
        )

        climate = analysis["climate"]
        soil = analysis["soil"]

        c1, c2, c3 = st.columns(3)
        temp = climate.get("temp_c")
        rain = climate.get("rain_mm_year")
        ph = soil.get("ph")
        c1.metric("Average temperature", f"{temp:.1f} °C" if temp is not None else "Unavailable")
        c2.metric("Annual rainfall", f"{rain:,.0f} mm" if rain is not None else "Unavailable")
        c3.metric("Soil pH", f"{ph:.2f}" if ph is not None else "Unavailable")

        terrain1, terrain2 = st.columns(2)
        with terrain1:
            elevation = soil.get("elevation_m")
            st.metric("Terrain elevation", f"{elevation:.0f} m" if elevation is not None else "Unavailable")
        with terrain2:
            slope = soil.get("slope_pct")
            st.metric("Nearby slope estimate", f"{slope:.1f}%" if slope is not None else "Unavailable")
        st.caption("Terrain uses a 90 m digital elevation model. The slope value is a rough estimate from nearby points, not a field survey.")
        st.caption(f"Data source: {analysis.get('data_mode', 'unknown')} • Soil pH and terrain are approximate screening signals.")

        st.markdown("")

        st.subheader("Factor breakdown")

        for factor_name, factor in analysis["factors"].items():

            value = factor["value"]
            low = factor["low"]
            high = factor["high"]
            score_value = factor["score"]
            unit = factor["unit"]

            status = factor_status(score_value)

            with st.container(border=True):
                factor_col, score_col = st.columns([3, 1])
                with factor_col:
                    st.markdown(f"**{factor_name}**")
                    st.caption(
                        f"Observed: {format_factor_value(value, unit)}  ·  "
                        f"Preferred: {low:g}–{high:g} {unit}  ·  {status}"
                    )
                with score_col:
                    st.metric("Screening fit", f"{score_percent(score_value)}%")
                if score_value is not None:
                    st.progress(max(0.0, min(1.0, float(score_value))))

        st.subheader("Why this result")

        for reason in analysis["reasons"]:
            st.write(f"- {reason}")

        if climate.get("error"):
            st.warning(climate["error"])

        if soil.get("error"):
            st.warning(soil["error"])
        if soil.get("terrain_error"):
            st.warning(soil["terrain_error"])
        render_voice_button(voice_field_summary(analysis))
    else:
        render_voice_button(
            tr("map_intro", language)
            + " "
            + tr("point_instruction", language)
        )


# ============================================================
# CROP FINDER
# ============================================================

elif st.session_state.page == "planner":

    st.subheader(tr("planner_title", language))

    st.write(tr("planner_intro", language))
    st.caption("Start with a field assessment to compare suitable crops, then build a companion plan from the screened pairings.")
    finder = st.session_state.get("crop_results")
    if not isinstance(finder, dict) or not finder.get("results"):
        point_note = (
            " "
            + tr("point_selected", language).format(
                lat=f"{st.session_state.planner_lat:.5f}",
                lon=f"{st.session_state.planner_lon:.5f}",
            )
            if st.session_state.planner_point_selected
            else " " + tr("point_instruction", language)
        )
        render_voice_button(tr("planner_intro", language) + point_note)

    st.markdown("#### Select a point on the planting map")
    planner_map_center = (
        [st.session_state.planner_lat, st.session_state.planner_lon]
        if st.session_state.planner_point_selected
        else [20.0, 0.0]
    )
    planner_map = folium.Map(
        location=planner_map_center,
        zoom_start=11 if st.session_state.planner_point_selected else 2,
        tiles=None if st.session_state.offline_mode else "OpenStreetMap",
        control_scale=True,
    )
    add_location_controls(planner_map)
    if st.session_state.planner_point_selected:
        folium.Marker(
            [st.session_state.planner_lat, st.session_state.planner_lon],
            tooltip="Selected planting point",
        ).add_to(planner_map)

    planner_map_data = st_folium(
        planner_map,
        height=430,
        width=None,
        returned_objects=["last_clicked"],
        key=f"terrasense_planner_map_{st.session_state.planner_map_generation}",
    )
    planner_coordinates = get_location_coordinates(planner_map_data)
    if planner_coordinates and st.session_state.planner_last_map_click != planner_coordinates:
        st.session_state.planner_last_map_click = planner_coordinates
        st.session_state.planner_lat, st.session_state.planner_lon = planner_coordinates
        st.session_state.planner_lat_input = planner_coordinates[0]
        st.session_state.planner_lon_input = planner_coordinates[1]
        st.session_state.planner_point_selected = True
        st.session_state.planner_point_source = "planner"
        st.session_state.planner_map_generation += 1
        st.session_state.crop_results = None
        st.rerun()

    if st.session_state.planner_point_selected:
        st.caption(
            f"Selected point: {st.session_state.planner_lat:.5f}, "
            f"{st.session_state.planner_lon:.5f}"
        )
    else:
        st.caption("Zoom and click the map to choose a planting location, or use your device location control.")

    if st.button(
        "Clear selected point",
        key="clear_planner_point",
        use_container_width=True,
        disabled=not st.session_state.planner_point_selected,
    ):
        st.session_state.planner_point_selected = False
        st.session_state.planner_point_source = None
        st.session_state.planner_map_generation += 1
        st.session_state.planner_last_map_click = None
        st.session_state.planner_lat = DEFAULT_LAT
        st.session_state.planner_lon = DEFAULT_LON
        st.session_state.planner_lat_input = DEFAULT_LAT
        st.session_state.planner_lon_input = DEFAULT_LON
        st.session_state.crop_results = None
        st.rerun()

    with st.expander("Enter planting coordinates manually"):
        c1, c2 = st.columns(2)
        with c1:
            finder_lat = st.number_input(
                "Latitude", min_value=-90.0, max_value=90.0, step=0.0001,
                format="%.5f", value=float(st.session_state.planner_lat), key="planner_lat_input",
                on_change=sync_planner_coordinates,
            )
        with c2:
            finder_lon = st.number_input(
                "Longitude", min_value=-180.0, max_value=180.0, step=0.0001,
                format="%.5f", value=float(st.session_state.planner_lon), key="planner_lon_input",
                on_change=sync_planner_coordinates,
            )

    if st.button(
        tr("find_crops", language),
        type="primary",
        use_container_width=True,
        disabled=not st.session_state.planner_point_selected,
    ):

        with st.spinner(
            "Analyzing the location..."
        ):

            try:

                manual = {
                    "enabled": st.session_state.get("manual_field_data", False),
                    "temp_c": st.session_state.get("manual_temperature"),
                    "rain_mm_year": st.session_state.get("manual_rainfall"),
                    "ph": st.session_state.get("manual_ph"),
                }
                climate, soil, data_mode = fetch_field_data(
                    finder_lat,
                    finder_lon,
                    offline=st.session_state.offline_mode,
                    manual=manual,
                )

                results = []

                for crop_name, thresholds in CROP_THRESHOLDS.items():

                    verdict, score, reasons = evaluate(
                        climate,
                        soil,
                        thresholds,
                    )

                    results.append(
                        {
                            "crop": crop_name,
                            "verdict": verdict,
                            "score": score,
                            "reasons": reasons,
                        }
                    )

                results.sort(
                    key=lambda item: (
                        item["score"]
                        if item["score"] is not None
                        else -1
                    ),
                    reverse=True,
                )

                st.session_state.crop_results = {
                    "climate": climate,
                    "soil": soil,
                    "results": results,
                    "latitude": finder_lat,
                    "longitude": finder_lon,
                    "data_mode": data_mode,
                }
                top_result = results[0] if results else {}
                save_activity(
                    "crop finder",
                    latitude=finder_lat,
                    longitude=finder_lon,
                    crop=top_result.get("crop"),
                    outcome=top_result.get("verdict"),
                    score=top_result.get("score"),
                    details={"data_mode": data_mode},
                )

            except Exception:

                st.error(
                    "We couldn't assess this location. Check the coordinates and "
                    "field values, then try again. If you are offline, use saved "
                    "data or enter local values."
                )

    finder = st.session_state.get("crop_results")

    if isinstance(finder, dict):

        results = finder.get("results", [])

        if results:

            st.divider()

            best_result = results[0]
            st.subheader("Plain-language summary")
            st.write(
                field_result_summary(
                    best_result["crop"],
                    best_result["verdict"],
                    best_result["score"],
                    language=language,
                )
            )
            st.subheader("Matching crops")

            for result in results[:12]:

                score = score_percent(
                    result["score"]
                )

                with st.container(border=True):
                    crop_col, score_col = st.columns([3, 1])
                    with crop_col:
                        st.markdown(f"**{result['crop']}**")
                        st.caption(result["verdict"])
                    with score_col:
                        st.metric("Screening fit", f"{score}%")
                    if result.get("score") is not None:
                        st.progress(max(0.0, min(1.0, float(result["score"]))))

            st.divider()
            st.subheader("Companion planting plan")
            main_crop = st.selectbox(
                "Choose the main crop for the companion plan",
                sorted(CROP_THRESHOLDS.keys()),
                index=(
                    sorted(CROP_THRESHOLDS.keys()).index(st.session_state.analysis["crop"])
                    if st.session_state.get("analysis")
                    and st.session_state.analysis.get("crop") in CROP_THRESHOLDS
                    else 0
                ),
                key="planner_main_crop",
            )
            companion_options = recommendations(main_crop, finder["climate"], finder["soil"])
            if companion_options:
                for option in companion_options:
                    percent = score_percent(option["score"])
                    st.markdown(f"### {safe_text(option['crop'])} · {percent}% site screen")
                    st.write(option["why"])
                    st.caption(f"Management: {option['manage']}")
                    st.caption(f"Site fit: {option['verdict']}. Review local spacing and planting dates before using this pairing.")
                    st.markdown(f"[Reference: extension guidance]({option['source']})")
            else:
                st.info("This first edition includes sourced examples for maize, green bean, pumpkin, cabbage, broccoli, carrot, and tomato. More locally reviewed pairings can be added.")
            st.warning(tr("screening_warning", language))
            voice_parts = [
                tr("planner_intro", language),
                field_result_summary(
                    best_result["crop"],
                    best_result["verdict"],
                    best_result["score"],
                    language=language,
                ),
                tr("top_crops", language) + ": " + "; ".join(
                    f"{item['crop']}, {score_percent(item['score'])}%"
                    for item in results[:5]
                ),
                tr("companion_plan", language).format(crop=main_crop),
            ]
            if companion_options:
                voice_parts.append(
                    tr("companion_crops", language).format(
                        crops=", ".join(item["crop"] for item in companion_options)
                    )
                )
            voice_parts.append(tr("screening_warning", language))
            render_voice_button(" ".join(voice_parts))


# ============================================================
# DISEASE AI
# ============================================================

elif st.session_state.page == "doctor":

    st.subheader(tr("doctor_title", language))

    st.write(tr("doctor_intro", language))
    st.warning(tr("screening_warning", language))
    doctor_crop_options = ["Choose a crop"] + sorted(CROP_THRESHOLDS.keys()) + ["Not sure"]
    doctor_crop = st.selectbox(
        "What crop is shown in the photo?",
        doctor_crop_options,
        key="doctor_crop_selection",
        on_change=clear_disease_results,
        help="The image model only covers crops in its training labels. If your crop is not supported, the app will say so instead of showing a forced nearest match.",
    )
    st.caption("For a crop-specific screen, select the crop before analyzing. Choose “Not sure” only to see the model’s closest trained class.")

    camera_photo = st.camera_input(tr("camera_photo", language))
    uploaded_file = camera_photo or st.file_uploader(
        tr("upload", language),
        type=[
            "jpg",
            "jpeg",
            "png",
            "webp",
        ],
    )

    if uploaded_file:

        try:

            image = Image.open(uploaded_file)

            st.image(
                image,
                caption="Uploaded image",
                use_container_width=True,
            )

            if st.button(
                tr("analyze_leaf", language),
                type="primary",
                use_container_width=True,
            ):

                if doctor_crop == "Choose a crop":
                    st.error("Select the crop shown in the photo before starting the screen.")
                else:
                    with st.spinner("Loading the leaf screening model..."):

                        try:

                            model = cached_disease_model(st.session_state.offline_mode)
                            is_uncertain_crop = doctor_crop == "Not sure"

                            if not is_uncertain_crop and not supports_plant(model, doctor_crop):
                                predictions = [{
                                    "plant": doctor_crop,
                                    "disease": "Unsupported crop",
                                    "confidence": None,
                                    "selected_crop": doctor_crop,
                                    "unsupported_crop": True,
                                    "supported_plants": supported_plant_names(model),
                                }]
                            else:
                                predictions = predict(
                                    image,
                                    model,
                                    top_k=3,
                                    plant_filter=None if is_uncertain_crop else doctor_crop,
                                )
                                for prediction in predictions:
                                    prediction["selected_crop"] = doctor_crop
                                    prediction["uncertain_crop"] = is_uncertain_crop

                            st.session_state.disease_results = predictions
                            best = predictions[0] if predictions else {}
                            save_activity(
                                "leaf screening",
                                crop=doctor_crop if best.get("unsupported_crop") else best.get("plant"),
                                outcome="Model does not cover selected crop" if best.get("unsupported_crop") else best.get("disease"),
                                score=best.get("confidence"),
                                details={
                                    "image_saved": False,
                                    "unsupported_crop": bool(best.get("unsupported_crop")),
                                    "crop_probability": best.get("crop_probability"),
                                    "global_class_probability": best.get("global_confidence"),
                                },
                            )

                        except Exception:

                            if st.session_state.offline_mode:
                                st.error("The model is not cached for offline use yet. Connect once, run a screening, and then retry offline.")
                            else:
                                st.error(
                                    "Leaf screening couldn't run. Try again later. If you "
                                    "are offline, connect once to download the model, then retry."
                                )

        except Exception:

            st.error(
                "We couldn't open that image. Choose a clear JPG, PNG, or WebP leaf photo and try again."
            )

    disease_results = st.session_state.get(
        "disease_results"
    )

    if disease_results:

        st.divider()

        st.subheader("Leaf screening result")

        best = disease_results[0]
        if best.get("unsupported_crop"):
            supported = ", ".join(best.get("supported_plants", [])) or "the crops listed by the model"
            message = tr("unsupported_crop", language).format(
                plant=best["plant"],
                supported=supported,
            )
            st.warning(message)
            st.caption(f"Crops represented in the model: {supported}.")
            render_voice_button(message + " " + tr("screening_warning", language))
        else:
            disease = best["disease"]
            plant = best["plant"]
            confidence = best["confidence"]
            is_uncertain_crop = best.get("uncertain_crop", False)
            crop_probability = best.get("crop_probability")
            low_crop_support = best.get("low_crop_support", False)

            st.subheader("Plain-language result")
            if low_crop_support:
                result_text = tr("disease_weak_support", language).format(
                    plant=best.get("selected_crop", plant),
                    overall_disease=best.get("overall_disease", "Unknown"),
                    overall_plant=best.get("overall_plant", "Unknown crop"),
                    overall_score=model_probability_label(best.get("overall_confidence")),
                    disease=disease,
                    score=model_probability_label(confidence),
                )
            elif disease == "Unknown" or plant == "Unknown crop":
                result_text = tr("disease_unknown", language)
            elif is_uncertain_crop:
                result_text = tr("disease_uncertain", language).format(
                    disease=disease,
                    plant=plant,
                )
            else:
                result_text = leaf_result_summary(
                    plant, disease, confidence, crop_probability, language
                )
            st.write(result_text)

            if disease != "Unknown" and plant != "Unknown crop":
                if is_uncertain_crop:
                    st.metric("Overall model class probability", model_probability_label(confidence))
                else:
                    score_col, support_col = st.columns(2)
                    score_col.metric(
                        tr("disease_class_score", language),
                        model_probability_label(confidence),
                    )
                    support_col.metric(
                        tr("disease_crop_support", language),
                        model_probability_label(crop_probability),
                    )
                    st.caption(tr("disease_score_note", language))

            st.subheader(tr("treatment_title", language))
            if low_crop_support:
                treatment_text = tr("disease_weak_treatment", language)
                st.info(treatment_text)
            elif disease == "Unknown":
                treatment_text = tr("disease_unknown", language)
                st.info(treatment_text)
            else:
                treatment_text = first_steps(disease)
                st.info(treatment_text)
            st.caption("The image is used for this screening and is not written to the history database. Hosted deployments still receive the upload for local inference.")
            st.caption("General first steps follow [University of Minnesota Extension disease-prevention guidance](https://extension.umn.edu/garden-and-home/yard-and-garden/gardening-in-minnesota/yard-and-garden-problems/preventing-plant-diseases-in-the-garden). See [UC IPM tomato mosaic guidance](https://ipm.ucanr.edu/agriculture/tomato/tobacco-mosaic/) and [Oregon State Extension apple scab guidance](https://extension.oregonstate.edu/es/node/123546/printable/print) for those examples. Local diagnosis and treatment rules vary.")
            render_voice_button(
                " ".join(
                    (
                        result_text,
                        tr("disease_next_steps", language),
                        tr("screening_warning", language),
                    )
                )
            )

            if len(disease_results) > 1 and not low_crop_support:
                with st.expander("Other possibilities"):
                    for result in disease_results[1:]:
                        st.write(
                            f"{result['plant']} — {result['disease']} "
                            f"({model_probability_label(result['confidence'])})"
                        )
    else:
        render_voice_button(tr("doctor_intro", language) + " " + tr("screening_warning", language))


# ============================================================
# ABOUT
# ============================================================

elif st.session_state.page == "history":

    st.subheader(tr("history_title", language))
    st.write(tr("history_intro", language))
    history_voice = [tr("history_intro", language)]
    if not st.session_state.username:
        st.info("Sign in from the sidebar to view saved activity. Guest activity remains only in the current session.")
        history_voice.append(tr("history_login", language))
    elif not DB_READY:
        st.error("Local history storage is unavailable on this installation.")
        history_voice.append("Saved activity is unavailable on this installation.")
    else:
        history_rows = get_history(st.session_state.username)
        if not history_rows:
            st.info(tr("no_history", language))
            history_voice.append(tr("no_history", language))
        else:
            for entry in history_rows:
                title = {
                    "field suitability": tr("activity_field", language),
                    "crop finder": tr("activity_crops", language),
                    "leaf screening": tr("activity_leaf", language),
                }.get(entry.get("entry_type"), entry.get("entry_type", "Activity").title())
                st.markdown(f"### {title}")
                details = [entry.get("created_at", "")]
                if entry.get("crop"):
                    details.append(str(entry["crop"]))
                if entry.get("outcome"):
                    details.append(str(entry["outcome"]))
                if entry.get("score") is not None:
                    details.append(f"{score_percent(entry['score'])}%")
                if entry.get("latitude") is not None and entry.get("longitude") is not None:
                    details.append(f"{entry['latitude']:.5f}, {entry['longitude']:.5f}")
                st.caption(" · ".join(details))
                if len(history_voice) < 6:
                    history_voice.append(f"{title}: " + ", ".join(details[1:]))
                st.divider()
    render_voice_button(" ".join(history_voice))

elif st.session_state.page == "share":

    st.subheader(tr("share_title", language))
    st.write(tr("share_description", language))
    public_url = str(st.context.url).strip()
    qr_image = qrcode.make(public_url)
    qr_buffer = io.BytesIO()
    qr_image.save(qr_buffer, format="PNG")
    qr_bytes = qr_buffer.getvalue()
    st.image(qr_bytes, caption=tr("scan_qr", language), width=240)
    st.download_button(
        tr("download_qr", language),
        qr_bytes,
        file_name="terrasense-app-qr.png",
        mime="image/png",
    )
    render_copy_link_button(public_url)
    share_voice = tr("share_description", language) + " " + public_url
    render_voice_button(share_voice)

elif st.session_state.page == "impact":

    st.subheader(tr("impact_title", language))
    with st.container(border=True):
        st.markdown("#### About Terrasense")
        st.write(
            "Terrasense addresses a practical knowledge gap: farmers need clear field information before choosing a crop or treatment. It combines coordinate-based climate and soil signals with a transparent crop screen, companion planting prompts, and a low-cost first response to leaf symptoms."
        )
        st.write(
            "The aim is to support better use of land and help farmers compare options that may improve crop production with lower cost and environmental pressure. The app does not promise a specific yield."
        )

    st.markdown(f"### {tr('sdg_title', language)}")
    sdg_goals = (
        ("2", "Zero Hunger", "Supports crop choices and food production decisions."),
        ("12", "Responsible Consumption and Production", "Promotes efficient use of soil, water, and inputs."),
        ("13", "Climate Action", "Uses climate information to guide field planning."),
        ("15", "Life on Land", "Encourages soil care and diverse planting systems."),
    )
    sdg_cols = st.columns(2)
    for index, (number, title, description) in enumerate(sdg_goals):
        with sdg_cols[index % 2]:
            with st.container(border=True):
                st.markdown(f"**UN SDG {number}**")
                st.markdown(f"**{title}**")
                st.write(description)
                st.markdown(f"[Official UN Goal {number}](https://sdgs.un.org/goals/goal{number})")
    st.caption(tr("sdg_note", language))

    with st.container(border=True):
        st.markdown(f"#### {tr('privacy_title', language)}")
        st.write(tr("privacy_body", language))
        st.write(tr("croppy_cloud_privacy", language))
        st.write(tr("croppy_local_privacy", language))
    with st.container(border=True):
        st.markdown(f"#### {tr('terms_title', language)}")
        st.write(tr("terms_body", language))
    with st.container(border=True):
        st.markdown("#### Free and offline use")
        st.write(tr("offline_detail", language))
        st.caption(tr("language_note", language))

    with st.container(border=True):
        st.markdown("#### Data sources")
        st.markdown(
            """
            - **NASA POWER** — free public climate data (online refresh)
            - **SoilGrids / ISRIC** — soil pH estimates (online refresh)
            - **OpenStreetMap** — online map tiles
            - **Open-Meteo Elevation API / Copernicus GLO-90** — terrain elevation and rough slope estimate; attribution required
            - **PlantVillage** — source dataset for the leaf screening model
            - **Hugging Face** — model files; download once before offline use
            - **Croppy** — optional Gemini Cloud API or a local Ollama model; cloud limits and pricing depend on the Google API project
            - **Core field features** run without paid API keys; hosting and device costs depend on deployment
            """
        )

    with st.container(border=True):
        st.markdown("#### How suitability is calculated")
        st.write("Terrasense compares average temperature, annual rainfall, and soil pH.")
        st.write(
            "The available factors are combined into a transparent screening score. It does not account for every farm variable, including local varieties, irrigation, slope, soil depth, pests, market access, or planting date."
        )
        st.caption("Long-term climate estimates are regional baselines, not farm sensor readings or a true microclimate model. Soil pH and terrain estimates are not a laboratory test or field survey.")
        st.info("The result is a screening indicator, not a guaranteed prediction of crop yield.")

    with st.container(border=True):
        st.markdown("#### How Terrasense works")
        st.markdown(
            """
            **1. Map the field** — choose a point and review available climate and soil estimates.

            **2. Compare crops** — use the suitability screen and see which factors affected the score.

            **3. Plan companion crops** — review sourced pairings and check each companion crop against the same field conditions.

            **4. Check leaf symptoms** — screen supported crop classes and review low-cost first steps.

            **5. Save a history** — sign in to keep your results in the local database on this installation.
            """
        )

    render_voice_button(
        tr("impact_voice", language)
        + ". "
        + tr("sdg_title", language)
        + ". "
        + tr("privacy_body", language)
        + " " + tr("terms_body", language)
        + " " + tr("offline_detail", language)
        + " " + tr("language_note", language)
    )


# ============================================================
# FOOTER
# ============================================================

st.divider()

st.caption(
    "Terrasense • Reboot the Earth 2026 • "
    "Challenge 1 • Team 17"
)

if st.session_state.croppy_open:
    with st.container(key="croppy_panel", border=True):
        header_col, close_col = st.columns([5, 1])
        with header_col:
            st.markdown(f"### {tr('croppy_title', language)}")
        with close_col:
            if st.button("×", key="croppy_close_button", help=tr("croppy_close", language)):
                st.session_state.croppy_open = False
                st.rerun()

        st.caption(tr("croppy_intro", language))
        st.selectbox(
            tr("croppy_mode", language),
            options=["cloud", "local"],
            format_func=lambda provider: tr(
                "croppy_cloud" if provider == "cloud" else "croppy_local", language
            ),
            key="croppy_provider",
        )

        if st.session_state.croppy_provider == "cloud":
            st.caption(tr("croppy_cloud_privacy", language))
            if st.session_state.offline_mode:
                st.warning(tr("croppy_cloud_offline", language))
            if not croppy_setting("GEMINI_API_KEY"):
                st.info(tr("croppy_setup_cloud", language))
                st.markdown("[Google AI Studio · create an API key](https://aistudio.google.com/app/apikey)")
            st.caption("[Gemini API pricing and limits](https://ai.google.dev/gemini-api/docs/pricing)")
        else:
            st.caption(tr("croppy_local_privacy", language))
            st.info(tr("croppy_setup_local", language))
            st.markdown("[Ollama download](https://ollama.com/download)")

        with st.container(height=360, border=True):
            if not st.session_state.croppy_messages:
                st.caption(tr("croppy_intro", language))
            for chat_message in st.session_state.croppy_messages:
                with st.chat_message(chat_message["role"]):
                    st.markdown(chat_message["content"])

        if st.session_state.croppy_last_error:
            st.error(st.session_state.croppy_last_error)
        st.caption(tr("croppy_disclaimer", language))

        with st.form("croppy_chat_form", clear_on_submit=True):
            prompt = st.text_input(
                tr("croppy_placeholder", language),
                key="croppy_prompt_input",
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button(
                tr("croppy_send", language), use_container_width=True
            )
        if submitted and prompt.strip():
            st.session_state.croppy_messages.append(
                {"role": "user", "content": prompt.strip()}
            )
            try:
                with st.spinner(tr("croppy_thinking", language)):
                    answer = croppy_reply(prompt.strip(), language)
                st.session_state.croppy_messages.append(
                    {"role": "assistant", "content": answer}
                )
                st.session_state.croppy_last_error = None
            except Exception as error:
                st.session_state.croppy_last_error = (
                    f"{tr('croppy_error', language)} {error}"
                )
            st.session_state.croppy_messages = st.session_state.croppy_messages[-24:]
            st.rerun()

        if st.button(tr("croppy_clear", language), key="croppy_clear_button"):
            st.session_state.croppy_messages = []
            st.session_state.croppy_last_error = None
            st.rerun()
elif st.button(
    tr("croppy_button", language),
    key="croppy_launcher",
    use_container_width=True,
    type="primary",
):
    st.session_state.croppy_open = True
    st.rerun()
