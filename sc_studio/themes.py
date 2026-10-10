"""Named colour schemes for the editor.

The palettes are the widely published community schemes -- Nord, Catppuccin,
Dracula, Monokai, One Dark, GitHub, Solarized, Gruvbox, Tokyo Night -- and each
scheme's colour values come from its own project. The mapping onto Qt palette
roles below is this project's own, so no third-party code or data is bundled.

Each scheme is described by a handful of core colours; the bevel and border
roles are derived from the background so a scheme stays compact and consistent.
"""

import colorsys

from PyQt6.QtGui import QColor, QPalette

BASE, SURFACE, TEXT, MUTED = "bg", "surface", "text", "muted"

SCHEMES = {
    # Dark schemes
    "Nord": {
        "bg": "#2e3440", "surface": "#3b4252", "text": "#eceff4",
        "muted": "#7b88a1", "accent": "#88c0d0", "link": "#81a1c1",
        "link_visited": "#b48ead", "tooltip_bg": "#3b4252",
        "tooltip_text": "#eceff4", "disabled": "#4c566a",
    },
    "Catppuccin Mocha": {
        "bg": "#1e1e2e", "surface": "#313244", "text": "#cdd6f4",
        "muted": "#a6adc8", "accent": "#89b4fa", "link": "#89dceb",
        "link_visited": "#cba6f7", "tooltip_bg": "#313244",
        "tooltip_text": "#cdd6f4", "disabled": "#6c7086",
    },
    "Catppuccin Macchiato": {
        "bg": "#24273a", "surface": "#363a4f", "text": "#cad3f5",
        "muted": "#a5adcb", "accent": "#8aadf4", "link": "#91d7e3",
        "link_visited": "#c6a0f6", "tooltip_bg": "#363a4f",
        "tooltip_text": "#cad3f5", "disabled": "#6e738d",
    },
    "Catppuccin Frappe": {
        "bg": "#303446", "surface": "#414559", "text": "#c6d0f5",
        "muted": "#a5adce", "accent": "#8caaee", "link": "#99d1db",
        "link_visited": "#ca9ee6", "tooltip_bg": "#414559",
        "tooltip_text": "#c6d0f5", "disabled": "#737994",
    },
    "Dracula": {
        "bg": "#282a36", "surface": "#44475a", "text": "#f8f8f2",
        "muted": "#8b93b8", "accent": "#bd93f9", "link": "#8be9fd",
        "link_visited": "#ff79c6", "tooltip_bg": "#44475a",
        "tooltip_text": "#f8f8f2", "disabled": "#6272a4",
    },
    "Monokai": {
        "bg": "#272822", "surface": "#3e3d32", "text": "#f8f8f2",
        "muted": "#9a9690", "accent": "#a6e22e", "link": "#66d9ef",
        "link_visited": "#f92672", "tooltip_bg": "#3e3d32",
        "tooltip_text": "#f8f8f2", "disabled": "#75715e",
    },
    "One Dark": {
        "bg": "#282c34", "surface": "#3e4451", "text": "#abb2bf",
        "muted": "#7f848e", "accent": "#61afef", "link": "#56b6c2",
        "link_visited": "#c678dd", "tooltip_bg": "#3e4451",
        "tooltip_text": "#abb2bf", "disabled": "#5c6370",
    },
    "GitHub Dark": {
        "bg": "#0d1117", "surface": "#161b22", "text": "#c9d1d9",
        "muted": "#8b949e", "accent": "#58a6ff", "link": "#58a6ff",
        "link_visited": "#bc8cff", "tooltip_bg": "#161b22",
        "tooltip_text": "#c9d1d9", "disabled": "#484f58",
    },
    "Solarized Dark": {
        "bg": "#002b36", "surface": "#073642", "text": "#93a1a1",
        "muted": "#657b83", "accent": "#268bd2", "link": "#2aa198",
        "link_visited": "#6c71c4", "tooltip_bg": "#073642",
        "tooltip_text": "#93a1a1", "disabled": "#586e75",
    },
    "Gruvbox Dark": {
        "bg": "#282828", "surface": "#3c3836", "text": "#ebdbb2",
        "muted": "#a89984", "accent": "#83a598", "link": "#8ec07c",
        "link_visited": "#d3869b", "tooltip_bg": "#3c3836",
        "tooltip_text": "#ebdbb2", "disabled": "#928374",
    },
    "Tokyo Night": {
        "bg": "#1a1b26", "surface": "#292e42", "text": "#c0caf5",
        "muted": "#737aa2", "accent": "#7aa2f7", "link": "#7dcfff",
        "link_visited": "#bb9af7", "tooltip_bg": "#292e42",
        "tooltip_text": "#c0caf5", "disabled": "#565f89",
    },
    # Light schemes
    "Catppuccin Latte": {
        "bg": "#eff1f5", "surface": "#ccd0da", "text": "#4c4f69",
        "muted": "#6c6f85", "accent": "#1e66f5", "link": "#04a5e5",
        "link_visited": "#8839ef", "tooltip_bg": "#e6e9ef",
        "tooltip_text": "#4c4f69", "disabled": "#9ca0b0",
    },
    "GitHub Light": {
        "bg": "#ffffff", "surface": "#f6f8fa", "text": "#24292f",
        "muted": "#57606a", "accent": "#0969da", "link": "#0969da",
        "link_visited": "#8250df", "tooltip_bg": "#f6f8fa",
        "tooltip_text": "#24292f", "disabled": "#8c959f",
    },
    "Solarized Light": {
        "bg": "#fdf6e3", "surface": "#eee8d5", "text": "#657b83",
        "muted": "#93a1a1", "accent": "#268bd2", "link": "#2aa198",
        "link_visited": "#6c71c4", "tooltip_bg": "#eee8d5",
        "tooltip_text": "#657b83", "disabled": "#93a1a1",
    },
    "Gruvbox Light": {
        "bg": "#fbf1c7", "surface": "#ebdbb2", "text": "#3c3836",
        "muted": "#7c6f64", "accent": "#076678", "link": "#427b58",
        "link_visited": "#8f3f71", "tooltip_bg": "#ebdbb2",
        "tooltip_text": "#3c3836", "disabled": "#a89984",
    },
    "One Light": {
        "bg": "#fafafa", "surface": "#eaeaeb", "text": "#383a42",
        "muted": "#696c77", "accent": "#4078f2", "link": "#0184bc",
        "link_visited": "#a626a4", "tooltip_bg": "#eaeaeb",
        "tooltip_text": "#383a42", "disabled": "#a0a1a7",
    },
}


def names():
    """Scheme names in menu order."""
    return list(SCHEMES)


def is_dark(name):
    """Whether a scheme is intended as a dark theme."""
    scheme = SCHEMES[name]
    return QColor(scheme[TEXT]).lightness() > QColor(scheme[BASE]).lightness()


def _offset(color, dark_factor, light_factor, dark):
    """Nudge a colour away from its background without saturating at the edge."""
    shifted = (
        color.darker(dark_factor) if dark else color.lighter(light_factor)
    )
    if shifted.rgb() == color.rgb():
        shifted = (
            color.lighter(160) if dark else color.darker(105)
        )
    return shifted


def _readable_on(background):
    """Black or white, whichever reads better on the given colour."""
    return QColor("#101318") if background.lightness() > 150 else QColor("#ffffff")


def role_colors(name):
    """Map a scheme onto the QPalette roles the editor sets.

    Returns the same shape the built-in themes use, plus ``disabled_text``,
    which is applied only to the disabled colour group.
    """
    scheme = SCHEMES[name]
    bg = QColor(scheme[BASE])
    surface = QColor(scheme[SURFACE])
    text = QColor(scheme[TEXT])
    accent = QColor(scheme["accent"])
    dark = is_dark(name)
    # Content areas sink below the window on dark schemes and lift above it on
    # light ones, so editors and lists read as distinct from the chrome. A
    # scheme whose background is already pure white (or black) saturates in that
    # direction, so step the other way to keep the two apart.
    base = _offset(bg, 115, 103, dark)
    return {
        "Window": bg.name(),
        "WindowText": text.name(),
        "Base": base.name(),
        "AlternateBase": surface.name(),
        "Text": text.name(),
        "Button": surface.name(),
        "ButtonText": text.name(),
        "Highlight": accent.name(),
        "HighlightedText": _readable_on(accent).name(),
        "ToolTipBase": scheme["tooltip_bg"],
        "ToolTipText": scheme["tooltip_text"],
        "Link": scheme["link"],
        "LinkVisited": scheme["link_visited"],
        "PlaceholderText": scheme["muted"],
        "BrightText": _readable_on(bg).name(),
        # Bevels and borders derive from the background so each scheme keeps a
        # consistent depth without listing five more colours.
        "Light": bg.lighter(160).name(),
        "Midlight": bg.lighter(130).name(),
        "Mid": bg.lighter(112).name(),
        "Dark": bg.darker(130).name(),
        "Shadow": bg.darker(160).name(),
        "disabled_text": scheme["disabled"],
    }


def muted_text(name):
    """The subdued colour used for secondary labels in a scheme."""
    return SCHEMES[name]["muted"]
