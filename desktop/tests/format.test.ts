import { afterEach, describe, expect, it } from "vitest";
import { isPlaceholderPreview, previewText } from "../src/i18n/format";
import { DEFAULT_LOCALE, setLocale } from "../src/i18n";

afterEach(() => setLocale(DEFAULT_LOCALE));

describe("placeholder previews", () => {
  it("says a clip with no text of its own in the reader's language", () => {
    // The sidecar stores the label, not the user's words: an image is previewed
    // by the literal "[Image]", and the window used to print that verbatim.
    expect(previewText("[Image]")).toBe("图片");
    expect(previewText("[Vector Image]")).toBe("矢量图");
    expect(previewText("[Rich Text]")).toBe("富文本");

    setLocale("en");
    expect(previewText("[Image]")).toBe("Image");
    expect(previewText("[Vector Image]")).toBe("Vector Image");
    expect(previewText("[Rich Text]")).toBe("Rich Text");
  });

  it("follows the locale after the module has loaded", () => {
    // The labels are thunks for this reason: a `t("图片")` evaluated at import
    // time would freeze the window into whichever language it started in.
    setLocale("en");
    expect(previewText("[Image]")).toBe("Image");
    setLocale("zh-CN");
    expect(previewText("[Image]")).toBe("图片");
  });

  it("passes every real preview through untouched", () => {
    // Anything the user actually copied is shown as it was copied, whatever it
    // looks like -- only the four labels the database writes are translated,
    // and three of them are.  "[HTML]" is the format's own name in both
    // languages, so it is deliberately not one of them.
    expect(previewText("[HTML]")).toBe("[HTML]");
    expect(previewText("hello")).toBe("hello");
    expect(previewText("")).toBe("");
    expect(previewText(null)).toBe("");
    expect(previewText(undefined)).toBe("");
    setLocale("en");
    expect(previewText("[IMAGE]")).toBe("[IMAGE]");
    expect(previewText("[image]")).toBe("[image]");
  });

  it("survives a preview that names a member of Object.prototype", () => {
    // The map is keyed by stored content, so a clip whose text is exactly
    // "constructor" reaches the lookup -- and must come back as itself rather
    // than as a function on its way to the screen.
    expect(previewText("constructor")).toBe("constructor");
    expect(previewText("toString")).toBe("toString");
    expect(previewText("hasOwnProperty")).toBe("hasOwnProperty");
  });

  it("tells a label apart from a clip that has more of it to show", () => {
    // What the hover card is gated on: a placeholder has no rest to reveal, a
    // cut-off clip does.
    expect(isPlaceholderPreview("[Image]")).toBe(true);
    expect(isPlaceholderPreview("[Vector Image]")).toBe(true);
    expect(isPlaceholderPreview("[Rich Text]")).toBe(true);
    expect(isPlaceholderPreview("[HTML]")).toBe(false);
    expect(isPlaceholderPreview("[Image] and then some")).toBe(false);
    expect(isPlaceholderPreview("")).toBe(false);
    expect(isPlaceholderPreview(null)).toBe(false);
    expect(isPlaceholderPreview(undefined)).toBe(false);
  });
});
