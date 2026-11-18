"""Design-system guards for the editor UI.

The editor ships as an offline desktop app, so its look must not depend on the
network, and its visual language must come from tokens rather than one-off
values scattered through a 7,000-line stylesheet.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
UI_ROOT = PROJECT_ROOT / "ui_prototype"
BOARD_HTML = UI_ROOT / "board.html"
BOARD_CSS = UI_ROOT / "board.css"
FONT_ROOT = UI_ROOT / "vendor" / "fonts"

WOFF2_MAGIC = b"wOF2"


def board_html() -> str:
    return BOARD_HTML.read_text(encoding="utf-8")


def board_style_block() -> str:
    """The stylesheet that carries the whole editor theme."""

    return BOARD_CSS.read_text(encoding="utf-8")


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(component) for component in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (relative_luminance(hex_to_rgb(foreground)), relative_luminance(hex_to_rgb(background))),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    digits = value.lstrip("#")
    return tuple(int(digits[index : index + 2], 16) / 255 for index in (0, 2, 4))  # type: ignore[return-value]


def root_tokens() -> dict[str, str]:
    root = re.search(r":root\{(.*?)\n  \}", board_style_block(), re.DOTALL)
    assert root is not None, "board.html must define a :root token block"
    return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", root.group(1)))


def dark_theme_tokens() -> dict[str, str]:
    """`body.dark` overrides part of the palette; the rest inherits from :root."""

    block = re.search(r"body\.dark\{(.*?)\n  \}", board_style_block(), re.DOTALL)
    assert block is not None, "board.html must define the body.dark token block"
    tokens = root_tokens()
    tokens.update(dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", block.group(1))))
    return tokens


# Surfaces a token-coloured text run can land on, per theme.
LIGHT_SURFACE_TOKENS = ("--surface", "--surface-2", "--bg", "--accent-soft")
# The chalkboard preview keeps its own dark surfaces inside the light theme.
CHALKBOARD_SURFACE_TOKENS = ("--board", "--board-2")
CHALK_TEXT_TOKENS = ("--chalk", "--chalk-warm", "--chalk-blue", "--chalk-pink")
# Used as `color` only to tint disabled/hover glyphs and ::before separators,
# never to carry words. Kept out of the AA sweep on purpose.
DECORATIVE_COLOR_TOKENS = ("--line", "--line-strong")
# Labels painted on a solid --ink fill. Checked by TestFilledSurfaceContrast
# against that fill, not against the page surfaces.
ON_FILL_TEXT_TOKENS = ("--on-ink",)
WCAG_AA_NORMAL_TEXT = 4.5
# Korean glyphs need more vertical space than Latin at the same px size, so the
# editor floors body text here rather than at the 8.5-10.5px it used to use.
MIN_FONT_SIZE_PX = 11.0


class TestOfflineFontIndependence(unittest.TestCase):
    def test_board_html_loads_no_remote_assets(self) -> None:
        remote_refs = re.findall(r'(?:href|src)="(https?://[^"]+)"', board_html())

        self.assertEqual(
            [],
            remote_refs,
            "the packaged app runs offline; bundle these assets under ui_prototype/vendor instead",
        )

    def test_self_hosted_font_files_are_real_woff2(self) -> None:
        expected = (
            "PretendardVariable.woff2",
            "JetBrainsMonoVariable.woff2",
            "CaveatVariable.woff2",
        )

        for name in expected:
            with self.subTest(font=name):
                path = FONT_ROOT / name
                self.assertTrue(path.is_file(), f"missing bundled font: {name}")
                self.assertEqual(WOFF2_MAGIC, path.read_bytes()[:4])

    def test_every_bundled_font_ships_its_license(self) -> None:
        for name in ("OFL-Pretendard.txt", "OFL-JetBrainsMono.txt", "OFL-Caveat.txt"):
            with self.subTest(license=name):
                path = FONT_ROOT / name
                self.assertTrue(path.is_file(), f"missing font license: {name}")
                self.assertIn("SIL Open Font License", path.read_text(encoding="utf-8"))

    def test_font_face_weight_ranges_cover_every_weight_the_css_uses(self) -> None:
        css = board_style_block()
        declared = {int(value) for value in re.findall(r"font-weight:\s*(\d+)", css)}
        declared |= {int(value) for value in re.findall(r"font:\s*(\d{3})\s", css)}
        self.assertTrue(declared, "expected the stylesheet to declare font weights")

        fonts_css = (FONT_ROOT / "fonts.css").read_text(encoding="utf-8")
        ranges = [
            (int(low), int(high))
            for low, high in re.findall(r"font-weight:\s*(\d+)\s+(\d+);", fonts_css)
        ]
        self.assertEqual(3, len(ranges), "expected one weight range per bundled family")

        variable_low = min(low for low, _ in ranges)
        variable_high = max(high for _, high in ranges)
        for weight in sorted(declared):
            with self.subTest(weight=weight):
                self.assertGreaterEqual(weight, variable_low)
                self.assertLessEqual(weight, variable_high)


class TestTypeFamilyTokens(unittest.TestCase):
    def test_every_font_family_declaration_uses_a_token(self) -> None:
        css = board_style_block()
        declarations = re.findall(r"font-family:\s*([^;]+);", css)
        self.assertTrue(declarations, "expected font-family declarations")

        untokenized = [value.strip() for value in declarations if not value.strip().startswith("var(--font-")]

        self.assertEqual([], untokenized, "font stacks must come from --font-* tokens")

    def test_font_tokens_declare_korean_capable_fallbacks(self) -> None:
        css = board_style_block()
        sans = re.search(r"--font-sans:\s*([^;]+);", css)
        self.assertIsNotNone(sans)
        stack = sans.group(1)

        self.assertIn("Pretendard Variable", stack)
        for fallback in ("Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR"):
            with self.subTest(fallback=fallback):
                self.assertIn(fallback, stack)

    def test_handwriting_token_falls_back_to_a_korean_face(self) -> None:
        css = board_style_block()
        hand = re.search(r"--font-hand:\s*([^;]+);", css)
        self.assertIsNotNone(hand)

        # Caveat is Latin-only; Korean glyphs must not drop to a random cursive.
        self.assertIn("Pretendard Variable", hand.group(1))


if __name__ == "__main__":
    unittest.main()


class TestTextContrast(unittest.TestCase):
    """Every token used as a text colour must clear WCAG AA on its own surfaces."""

    def setUp(self) -> None:
        self.css = board_style_block()
        self.tokens = root_tokens()
        self.dark_tokens = dark_theme_tokens()

    def _text_colour_tokens(self) -> set[str]:
        return set(re.findall(r"color:\s*var\((--[a-z0-9-]+)\)\s*(?:!important)?;", self.css))

    def _sweep(self, palette, text_tokens, surface_tokens, label):
        failures = []
        for token in sorted(text_tokens):
            value = palette.get(token, "").strip()
            if not value.startswith("#"):
                continue
            for surface_token in surface_tokens:
                surface = palette[surface_token].strip()
                ratio = contrast_ratio(value, surface)
                if ratio < WCAG_AA_NORMAL_TEXT:
                    failures.append(f"[{label}] {token} ({value}) on {surface_token}: {ratio:.2f}")
        return failures

    def test_light_theme_text_tokens_clear_aa(self) -> None:
        text_tokens = (
            self._text_colour_tokens()
            - set(CHALK_TEXT_TOKENS)
            - set(DECORATIVE_COLOR_TOKENS)
            - set(ON_FILL_TEXT_TOKENS)
        )
        self.assertTrue(text_tokens, "expected token-coloured text")

        failures = self._sweep(self.tokens, text_tokens, LIGHT_SURFACE_TOKENS, "light")

        self.assertEqual([], failures, "text tokens below WCAG AA 4.5:1")

    def test_dark_theme_text_tokens_clear_aa(self) -> None:
        text_tokens = (
            self._text_colour_tokens()
            - set(CHALK_TEXT_TOKENS)
            - set(DECORATIVE_COLOR_TOKENS)
            - set(ON_FILL_TEXT_TOKENS)
        )

        failures = self._sweep(self.dark_tokens, text_tokens, LIGHT_SURFACE_TOKENS, "dark")

        self.assertEqual([], failures, "body.dark must override chromatic text tokens")

    def test_chalk_text_clears_aa_on_the_board(self) -> None:
        used = self._text_colour_tokens() & set(CHALK_TEXT_TOKENS)
        self.assertTrue(used, "expected chalk-coloured text on the board preview")

        failures = self._sweep(self.tokens, used, CHALKBOARD_SURFACE_TOKENS, "board")

        self.assertEqual([], failures, "chalk text must stay readable on the board")

    def test_decorative_tokens_only_tint_disabled_or_pseudo_elements(self) -> None:
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", self.css)
        leaked = []
        for selector, body in rules:
            for token in DECORATIVE_COLOR_TOKENS:
                if re.search(rf"color:\s*var\({token}\)\s*(?:!important)?;", body):
                    if not re.search(r":hover|:disabled|::before|::after|\.disabled|\[open\]|-move\b", selector):
                        leaked.append(f"{token} in {selector.strip()[:70]}")

        self.assertEqual(
            [],
            leaked,
            "hairline tokens are not readable as text; use --muted or --ink-2",
        )

    def test_low_contrast_decorative_token_is_never_text(self) -> None:
        # --muted-2 is a decorative grey (dots, hairlines). At 2.38:1 it is not
        # readable, so it must never be assigned to `color`.
        self.assertNotIn("--muted-2", self._text_colour_tokens())

    def test_status_and_accent_colours_have_readable_ink_variants(self) -> None:
        for base in ("--accent", "--ok", "--danger"):
            with self.subTest(token=base):
                ink = f"{base}-ink"
                self.assertIn(ink, self.tokens, f"expected a readable text variant {ink}")
                self.assertNotIn(
                    base,
                    self._text_colour_tokens(),
                    f"{base} paints fills and borders; text must use {ink}",
                )


class TestTypeScale(unittest.TestCase):
    def setUp(self) -> None:
        self.css = board_style_block()

    def setUpScale(self) -> dict[str, float]:
        return {
            name: float(value.rstrip("px"))
            for name, value in root_tokens().items()
            if name.startswith("--text-")
        }

    def _declared_sizes(self) -> list[float]:
        """Resolve every font size the stylesheet sets, through its token."""

        scale = self.setUpScale()
        raw = re.findall(r"font-size:\s*([^;]+);", self.css)
        raw += [
            match.group(1)
            for match in re.finditer(r"font:\s*(?:\d{3}\s+)?(var\(--text-[a-z0-9]+\)|[0-9.]+px)", self.css)
        ]

        sizes = []
        for value in raw:
            value = value.replace("!important", "").strip()
            token = re.fullmatch(r"var\((--text-[a-z0-9]+)\)", value)
            if token is not None:
                sizes.append(scale[token.group(1)])
            elif value.endswith("px"):
                sizes.append(float(value[:-2]))
            else:  # pragma: no cover - keeps an unexpected unit visible
                self.fail(f"unrecognised font-size value: {value}")
        return sizes

    def test_every_font_size_uses_a_scale_token(self) -> None:
        literals = [
            value.strip()
            for value in re.findall(r"font-size:\s*([^;]+);", self.css)
            if not value.strip().startswith("var(--text-")
        ]

        self.assertEqual([], literals, "font sizes must reference the --text-* scale")

    def test_no_text_is_smaller_than_the_legibility_floor(self) -> None:
        too_small = sorted({size for size in self._declared_sizes() if size < MIN_FONT_SIZE_PX})

        self.assertEqual([], too_small, f"font sizes below {MIN_FONT_SIZE_PX}px")

    def test_the_scale_has_no_half_pixel_steps(self) -> None:
        scale = self.setUpScale()
        self.assertTrue(scale, "expected a --text-* scale")

        fractional = sorted(name for name, size in scale.items() if size != int(size))
        self.assertEqual([], fractional, "type-scale steps must be whole pixels")

    def test_font_shorthand_uses_the_family_tokens(self) -> None:
        stacks = re.findall(r"font:\s*(?:\d{3}\s+)?(?:var\(--text-[a-z0-9]+\)|[0-9.]+px)(?:/[0-9.]+)?\s+([^;]+);", self.css)
        untokenized = [stack.strip() for stack in stacks if not stack.strip().startswith("var(--font-")]

        self.assertEqual([], untokenized, "font shorthand must use the --font-* family tokens")


class TestSpacingRhythm(unittest.TestCase):
    """Spacing sits on a 2px grid; the odd 1/3/5/7/9px values were drift."""

    SPACING_PROPERTIES = (
        "padding",
        "margin",
        "gap",
        "row-gap",
        "column-gap",
        "padding-top",
        "padding-right",
        "padding-bottom",
        "padding-left",
        "padding-block",
        "padding-inline",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "margin-block",
        "margin-inline",
    )

    def test_spacing_values_sit_on_the_two_pixel_grid(self) -> None:
        css = board_style_block()
        pattern = "|".join(re.escape(name) for name in self.SPACING_PROPERTIES)

        off_grid = []
        for match in re.finditer(rf"\b({pattern})\s*:\s*([^;]+);", css):
            value = match.group(2)
            if "var(" in value or "calc(" in value:
                continue
            for number in re.findall(r"(-?[0-9.]+)px", value):
                if abs(float(number)) % 2 != 0:
                    off_grid.append(f"{match.group(1)}: {value.strip()}")

        self.assertEqual([], sorted(set(off_grid)), "spacing must snap to the 2px grid")


class TestLayerScale(unittest.TestCase):
    def setUp(self) -> None:
        self.css = board_style_block()
        self.tokens = root_tokens()

    def test_global_overlays_use_named_layers(self) -> None:
        raw = [int(value) for value in re.findall(r"z-index:\s*(\d+);", self.css)]
        magic = sorted({value for value in raw if value > 20})

        self.assertEqual([], magic, "overlay z-indexes must use a --z-* layer token")

    def test_layer_tokens_are_ordered_and_reserve_room(self) -> None:
        layers = {
            name: int(value)
            for name, value in self.tokens.items()
            if name.startswith("--z-")
        }
        self.assertTrue(layers, "expected --z-* layer tokens")

        values = sorted(layers.values())
        self.assertEqual(len(values), len(set(values)), "layer tokens must be distinct")
        self.assertEqual(
            values,
            sorted(values),
            "layer tokens must read in stacking order",
        )
        for value in values:
            with self.subTest(layer=value):
                self.assertGreaterEqual(value, 100, "layer tokens live above local stacking")
                self.assertLess(value, 1000, "no magic 10000-style escape hatches")


class TestBreakpointSet(unittest.TestCase):
    CANONICAL_WIDTHS = {1280, 1100, 920, 700, 420}

    def test_width_queries_use_the_canonical_breakpoints(self) -> None:
        css = board_style_block()
        widths = {int(value) for value in re.findall(r"max-width:\s*(\d+)px\)", css)}
        widths |= {
            int(value) - 1 for value in re.findall(r"min-width:\s*(\d+)px\)", css)
        }

        self.assertEqual(
            set(),
            widths - self.CANONICAL_WIDTHS,
            f"width breakpoints must come from {sorted(self.CANONICAL_WIDTHS, reverse=True)}",
        )


class TestMotionScale(unittest.TestCase):
    def test_transition_durations_use_motion_tokens(self) -> None:
        css = board_style_block()

        literals = []
        for match in re.finditer(r"transition:\s*([^;]+);", css):
            value = match.group(1)
            literals += re.findall(r"(?<=\s)(\.\d+s|\d+m?s)", value)

        self.assertEqual([], sorted(set(literals)), "transition durations must use --motion-* tokens")

    def test_motion_tokens_are_a_three_step_scale(self) -> None:
        motion = {name: value for name, value in root_tokens().items() if name.startswith("--motion-")}

        self.assertEqual({"--motion-fast", "--motion-base", "--motion-slow"}, set(motion))
        durations = [int(value.strip().rstrip("ms")) for value in motion.values()]
        self.assertEqual(sorted(durations), sorted(set(durations)), "motion steps must be distinct")

    def test_one_spinner_speed(self) -> None:
        css = board_style_block()
        speeds = set(re.findall(r"animation:\s*spin\s+([0-9.]+m?s)", css))

        self.assertEqual(1, len(speeds), f"the spinner must have one speed, found {sorted(speeds)}")


class TestStylesheetSeparation(unittest.TestCase):
    def test_theme_lives_in_its_own_stylesheet(self) -> None:
        self.assertTrue(BOARD_CSS.is_file(), "expected ui_prototype/board.css")
        self.assertNotIn(
            "<style>",
            board_html(),
            "board.html must link board.css instead of inlining the theme",
        )

    def test_stylesheet_is_cache_busted_by_its_own_digest(self) -> None:
        import hashlib

        digest = hashlib.sha256(BOARD_CSS.read_bytes()).hexdigest()

        self.assertIn(
            f'href="board.css?v=board-css-{digest}"',
            board_html(),
            "board.css cache bust is stale; rebuild with scripts/build_frontend_bundle.mjs",
        )

    def test_the_dead_conflicting_stylesheet_is_gone(self) -> None:
        self.assertFalse(
            (UI_ROOT / "styles.css").exists(),
            "ui_prototype/styles.css was an unreferenced second design system",
        )


class TestFilledSurfaceContrast(unittest.TestCase):
    """A rule that paints both a fill and a label must stay legible in both
    themes. Hardcoding `color: white` on a `var(--ink)` fill inverts to
    white-on-white the moment the dark theme flips --ink."""

    NAMED_COLORS = {"white": "#ffffff", "#fff": "#ffffff", "black": "#000000", "#000": "#000000"}

    def setUp(self) -> None:
        self.css = board_style_block()
        self.rules = re.findall(r"([^{}]+)\{([^{}]*)\}", self.css)

    @staticmethod
    def _declaration(body: str, prop: str) -> str | None:
        match = re.search(rf"(?:^|;|\s){re.escape(prop)}:\s*([^;]+)", body)
        return match.group(1).strip() if match else None

    def _resolve(self, value: str | None, palette: dict[str, str]) -> str | None:
        if value is None:
            return None
        token = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value)
        if token is not None:
            value = palette.get(token.group(1), "").strip()
        value = self.NAMED_COLORS.get(value, value)
        return value if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else None

    def _dark_overrides(self) -> dict[str, dict[str, str]]:
        """Fill and label overrides declared as `body.dark <selector>`."""

        overrides: dict[str, dict[str, str]] = {}
        for selector, body in self.rules:
            selector = " ".join(selector.split())
            if not selector.startswith("body.dark "):
                continue
            entry = overrides.setdefault(selector[len("body.dark ") :], {})
            background = self._declaration(body, "background") or self._declaration(body, "background-color")
            if background:
                entry["background"] = background
            foreground = self._declaration(body, "color")
            if foreground:
                entry["color"] = foreground
        return overrides

    def test_filled_surfaces_stay_legible_in_both_themes(self) -> None:
        palettes = (("light", root_tokens()), ("dark", dark_theme_tokens()))
        overrides = self._dark_overrides()

        failures = []
        for selector, body in self.rules:
            selector = " ".join(selector.split())
            if selector.startswith("body.dark "):
                continue
            background = self._declaration(body, "background") or self._declaration(body, "background-color")
            foreground = self._declaration(body, "color")
            if background is None or foreground is None:
                continue
            for theme, palette in palettes:
                fill, text = background, foreground
                if theme == "dark":
                    override = overrides.get(selector, {})
                    fill = override.get("background", fill)
                    text = override.get("color", text)
                resolved_fill = self._resolve(fill, palette)
                resolved_text = self._resolve(text, palette)
                if resolved_fill is None or resolved_text is None:
                    continue
                ratio = contrast_ratio(resolved_text, resolved_fill)
                if ratio < WCAG_AA_NORMAL_TEXT:
                    failures.append(
                        f"[{theme}] {selector[:60]}: {foreground} on {background} = {ratio:.2f}"
                    )

        self.assertEqual([], failures, "filled surfaces below WCAG AA 4.5:1")

    def test_ink_fills_use_the_inverting_label_token(self) -> None:
        leaked = []
        for selector, body in self.rules:
            background = self._declaration(body, "background") or ""
            if "var(--ink)" not in background:
                continue
            foreground = self._declaration(body, "color")
            if foreground in ("white", "#fff", "#ffffff"):
                leaked.append(" ".join(selector.split())[:60])

        self.assertEqual(
            [],
            leaked,
            "an --ink fill must label itself with var(--on-ink), which inverts with the theme",
        )
