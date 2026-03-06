#!/usr/bin/env python3
"""
Paperturn Translation App
Streamlit front-end for the batch translation script.
"""

import os
import tempfile
import time
from pathlib import Path

import streamlit as st

from translate import (
    scrape_page,
    discover_urls,
    get_system_prompts,
    translate_content,
    translate_images,
    translate_seo,
    fetch_semrush_keywords,
    write_xlsx,
    get_safe_path,
    DEFAULT_OUTPUT,
)

try:
    import anthropic
except ImportError:
    st.error("Missing `anthropic` package. Run: pip install anthropic")
    st.stop()

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Paperturn Translator",
    page_icon="📄",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .stApp { max-width: 1000px; margin: 0 auto; }
    div[data-testid="stStatusWidget"] { display: none; }

</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Sidebar: API Keys
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("API Keys")
    anthropic_key = st.text_input(
        "Anthropic API Key",
        value=os.environ.get("ANTHROPIC_API_KEY", ""),
        type="password",
        help="Required for translation. Set ANTHROPIC_API_KEY env var to pre-fill.",
    )
    semrush_key = st.text_input(
        "SEMrush API Key (optional)",
        value=os.environ.get("SEMRUSH_API_KEY", ""),
        type="password",
        help="Only needed if you enable SEMrush keyword analysis.",
    )

# ---------------------------------------------------------------------------
# Main UI
# ---------------------------------------------------------------------------
st.title("Paperturn Translator")
st.caption("Scrape, translate, and copywrite Paperturn pages into any language.")

# --- Step 1: Language ---
st.subheader("1. Target Language")
lang = st.selectbox(
    "Which language are you translating into?",
    ["Spanish", "French", "German", "Danish", "Swedish", "Italian", "Portuguese", "Dutch"],
    index=0,
)
custom_lang = st.text_input("Or type a custom language:", "")
target_lang = custom_lang.strip() if custom_lang.strip() else lang

# --- Step 2: URLs ---
st.subheader("2. Pages to Translate")
url_mode = st.radio(
    "How would you like to provide URLs?",
    ["Paste URLs", "Upload a file", "Crawl a subpath", "Crawl entire site"],
    horizontal=True,
)

urls = []

if url_mode == "Paste URLs":
    url_text = st.text_area(
        "Paste one URL per line:",
        placeholder="https://www.paperturn.com/industries/manufacturing\nhttps://www.paperturn.com/industries/real-estate",
        height=150,
    )
    urls = [u.strip() for u in url_text.strip().splitlines() if u.strip() and not u.startswith("#")]

elif url_mode == "Upload a file":
    uploaded = st.file_uploader("Upload a .txt file with one URL per line", type=["txt"])
    if uploaded:
        content = uploaded.read().decode("utf-8")
        urls = [u.strip() for u in content.splitlines() if u.strip() and not u.startswith("#")]

elif url_mode == "Crawl a subpath":
    subpath = st.text_input("Subpath to crawl:", value="/industries/", help="e.g., /industries/, /features/")
    if subpath and st.button("Discover URLs"):
        with st.spinner("Discovering pages..."):
            urls = discover_urls(subpath=subpath)
        st.session_state["discovered_urls"] = urls
    if "discovered_urls" in st.session_state:
        urls = st.session_state["discovered_urls"]

elif url_mode == "Crawl entire site":
    if st.button("Discover all URLs"):
        with st.spinner("Discovering pages..."):
            urls = discover_urls(crawl_all=True)
        st.session_state["discovered_urls"] = urls
    if "discovered_urls" in st.session_state:
        urls = st.session_state["discovered_urls"]

if urls:
    st.success(f"**{len(urls)} page(s) ready to translate:**")
    for u in urls:
        st.text(f"  {u}")

# --- Step 3: Options ---
st.subheader("3. Options")
col1, col2 = st.columns(2)
with col1:
    enable_semrush = st.checkbox("Enable SEMrush keyword analysis", value=False)
    enable_char_limit = st.checkbox("Enforce character limits", value=False,
                                     help="Keep translated copy close to the English character count per tag.")
with col2:
    if enable_semrush and urls:
        semrush_urls = st.multiselect(
            "Select URLs for SEMrush analysis:",
            urls,
            default=[],
            help="Only selected URLs will be analyzed (costs API credits).",
        )
    else:
        semrush_urls = []
    if enable_char_limit:
        char_limit_pct = st.slider(
            "Max character overshoot allowed",
            min_value=0, max_value=30, value=10, step=5,
            format="%d%%",
            help="0% = exact match, 10% = allow 10% longer than English, etc.",
        )
    else:
        char_limit_pct = None

# --- Step 4: Run ---
st.subheader("4. Translate")

can_run = bool(urls) and bool(anthropic_key)
if not anthropic_key:
    st.warning("Enter your Anthropic API key in the sidebar to continue.")
if enable_semrush and not semrush_key:
    st.warning("Enter your SEMrush API key in the sidebar, or disable SEMrush.")

if st.button("Start Translation", disabled=not can_run, type="primary"):
    out_path = Path(tempfile.mkdtemp())
    client = anthropic.Anthropic(api_key=anthropic_key)

    progress_bar = st.progress(0)
    status = st.empty()
    results_container = st.container()

    total = len(urls)
    completed_files = []

    for i, url in enumerate(urls):
        page_num = i + 1
        status.markdown(f"**[{page_num}/{total}]** Processing: `{url}`")

        # Scrape
        try:
            status.markdown(f"**[{page_num}/{total}]** Scraping `{url}` ...")
            page_data = scrape_page(url)
        except Exception as e:
            st.error(f"Failed to scrape {url}: {e}")
            continue

        # Get prompts
        prompts = get_system_prompts(target_lang)

        # Content
        try:
            status.markdown(f"**[{page_num}/{total}]** Translating content ({len(page_data['content'])} items) ...")
            content_translations = translate_content(
                client, page_data["content"],
                prompts["translator"], prompts["copywriter"],
                target_lang, char_limit_pct=char_limit_pct,
            )
        except Exception as e:
            st.error(f"Content translation failed: {e}")
            content_translations = ([], [])

        # Images
        try:
            status.markdown(f"**[{page_num}/{total}]** Translating {len(page_data['images'])} image alt texts ...")
            image_translations = translate_images(
                client, page_data["images"],
                prompts["alt_translator"], prompts["alt_copywriter"],
                target_lang, char_limit_pct=char_limit_pct,
            )
        except Exception as e:
            st.error(f"Image translation failed: {e}")
            image_translations = ([], [])

        # SEO
        try:
            status.markdown(f"**[{page_num}/{total}]** Translating SEO metadata ...")
            seo_translations = translate_seo(
                client, page_data["seo"],
                prompts["seo_translator"], prompts["seo_copywriter"],
                target_lang, char_limit_pct=char_limit_pct,
            )
        except Exception as e:
            st.error(f"SEO translation failed: {e}")
            seo_translations = ([], [])

        # SEMrush
        semrush_keywords = []
        if enable_semrush and semrush_key and url in semrush_urls:
            status.markdown(f"**[{page_num}/{total}]** Fetching SEMrush keywords ...")
            semrush_keywords = fetch_semrush_keywords(semrush_key, url, target_lang)

        # Write Excel
        file_path = get_safe_path(out_path, page_data["page_name"], target_lang)
        try:
            write_xlsx(file_path, page_data, content_translations,
                       image_translations, seo_translations, semrush_keywords)
            completed_files.append(file_path)
        except Exception as e:
            st.error(f"Failed to write Excel: {e}")

        progress_bar.progress(page_num / total)

    # Done
    status.empty()
    progress_bar.empty()

    if completed_files:
        st.balloons()
        st.success(f"Done! {len(completed_files)} file(s) saved to `{output_dir}`")
        for f in completed_files:
            st.markdown(f"- `{f.name}`")

            # Offer download
            with open(f, "rb") as fh:
                st.download_button(
                    label=f"Download {f.name}",
                    data=fh.read(),
                    file_name=f.name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
    else:
        st.error("No files were generated. Check the errors above.")
