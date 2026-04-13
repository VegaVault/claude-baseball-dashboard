"""
Streamlit dashboard: MLB daily matchup viewer.

Reads JSON files from data/ — no live API calls.
All times displayed in ET (converted from UTC in the JSON).

Layout:
  Sidebar: date picker (today + yesterday for v1), game dropdown
  Main: header, reconciliation table, pitcher cards, lineup tables, status badge
"""

import streamlit as st


def main() -> None:
    st.set_page_config(page_title="MLB Dashboard", layout="wide")
    st.title("MLB Daily Dashboard")
    st.info("Dashboard coming in Phase 4.")


if __name__ == "__main__":
    main()
