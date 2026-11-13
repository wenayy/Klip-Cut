# Twemoji artwork

Color emoji used by the caption renderer, as 72×72 PNGs.

- Source: [Twemoji](https://github.com/jdecked/twemoji) (formerly Twitter Twemoji)
- License: [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- Filename: lowercase Unicode codepoint, no `FE0F` variation selector (the
  Twemoji convention), e.g. 😰 is `1f630.png`.

The renderer bundles the common set and fetches a missing codepoint once from
jsDelivr, caching it here. Set `CLIPPER_EMOJI_FETCH=0` to keep rendering fully
offline; emoji without local artwork are then omitted rather than drawn as an
empty glyph.
