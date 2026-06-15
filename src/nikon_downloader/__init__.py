"""Nikon Imaging Cloud — one-way image downloader / sync.

Package layout (each module is small and single-purpose):

    config      load settings from environment + credentials file
    models      ImageItem data model and parsing of the API response
    auth        browser-assisted login, token/request capture, token store
    api         thin client for the Nikon "BFF" data API
    downloader  download files into a YYYY/MM/DD layout, with resume
    sync        orchestration: list -> filter -> download, plus poll service
    cli         headless command-line entry points
    ui          optional NiceGUI control panel over the same engine
"""

__version__ = "0.1.0"
