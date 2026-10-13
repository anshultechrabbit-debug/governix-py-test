/**
 * An organization's branding in the tenant portal: its primary colour becomes the brand palette, its
 * secondary colour the sidebar, and its Google Font the page font. Master Admin keeps the Governix look.
 */
import type { CSSProperties } from "react";

export const HEX = /^#[0-9a-fA-F]{6}$/;
type RGB = [number, number, number];

function rgb(color: string): RGB {
  const n = parseInt(color.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function hex([r, g, b]: RGB) {
  return "#" + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("").toUpperCase();
}

/** `color` blended into `base`, `weight` being the share of `color`. */
function mix(color: string, base: string, weight: number) {
  const [a, b] = [rgb(color), rgb(base)];
  return hex(a.map((v, i) => v * weight + b[i] * (1 - weight)) as RGB);
}

/** WCAG relative luminance: 0 for black, 1 for white. */
export function luminance(color: string) {
  const [r, g, b] = rgb(color).map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function saturationAndLightness(color: string): [number, number] {
  const [r, g, b] = rgb(color).map((v) => v / 255);
  const [max, min] = [Math.max(r, g, b), Math.min(r, g, b)];
  const lightness = (max + min) / 2;
  return [max === min ? 0 : (max - min) / (1 - Math.abs(2 * lightness - 1)), lightness];
}

/** The `--color-brand-*` scale the theme uses, from one colour. Buttons put white text on 600,
 * so a light colour is darkened until that stays readable. */
function brandScale(primary: string): Record<string, string> {
  const base = luminance(primary) > 0.35 ? mix(primary, "#000000", 0.7) : primary;
  return {
    "--color-brand-50": mix(base, "#FFFFFF", 0.08),
    "--color-brand-100": mix(base, "#FFFFFF", 0.16),
    "--color-brand-500": mix(base, "#FFFFFF", 0.85),
    "--color-brand-600": base,
    "--color-brand-700": mix(base, "#000000", 0.8),
    "--color-brand-900": mix(base, "#000000", 0.45),
  };
}

export function fontStack(font: string) {
  return `"${font}", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`;
}

/** Adds the Google Fonts stylesheet for `font` once. Names come from the backend's list of fonts. */
export function loadGoogleFont(font: string) {
  const id = `google-font-${font.replace(/\s+/g, "-").toLowerCase()}`;
  if (document.getElementById(id)) return;
  const link = document.createElement("link");
  link.id = id;
  link.rel = "stylesheet";
  link.href = `https://fonts.googleapis.com/css2?family=${font.replace(/\s+/g, "+")}:wght@400;500;600;700&display=swap`;
  document.head.appendChild(link);
}

/** Applies the brand palette and font to the whole page; the returned function restores the defaults. */
export function applyBrand(brand: { primary_color: string | null; brand_font: string | null }): () => void {
  const vars: Record<string, string> = {};
  if (brand.primary_color && HEX.test(brand.primary_color)) Object.assign(vars, brandScale(brand.primary_color));
  if (brand.brand_font) {
    loadGoogleFont(brand.brand_font);
    vars["--font-sans"] = vars["font-family"] = fontStack(brand.brand_font);
  }
  const root = document.documentElement.style;
  for (const [name, value] of Object.entries(vars)) root.setProperty(name, value);
  return () => Object.keys(vars).forEach((name) => root.removeProperty(name));
}

/** Re-colours the sidebar with the secondary colour: light text on a dark colour, a tint on a light one.
 * Menus and dialogs opened from the sidebar carry `page-palette`, which restores the page colours (index.css). */
export function sidebarPalette(secondary: string | null | undefined): CSSProperties | undefined {
  if (!secondary || !HEX.test(secondary)) return undefined;
  if (luminance(secondary) > 0.4) {
    return {
      "--color-surface": secondary,
      "--color-subtle": mix(secondary, "#000000", 0.94),
      "--color-line": mix(secondary, "#000000", 0.88),
    } as CSSProperties;
  }
  return {
    "--color-surface": secondary,
    "--color-subtle": "rgb(255 255 255 / 0.08)",
    "--color-line": "rgb(255 255 255 / 0.12)",
    "--color-line-strong": "rgb(255 255 255 / 0.24)",
    "--color-ink": "#FFFFFF",
    "--color-ink-soft": "rgb(255 255 255 / 0.82)",
    "--color-muted": "rgb(255 255 255 / 0.6)",
  } as CSSProperties;
}

/** A logo's brand colours: its most common vivid colour, and its most common dark one (or a deep shade
 * of the vivid one). Either is missing when the logo has none. */
export async function logoColors(file: Blob): Promise<{ primary?: string; secondary?: string }> {
  const bitmap = await createImageBitmap(file);
  const size = 48;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return {};
  context.drawImage(bitmap, 0, 0, size, size);
  bitmap.close();
  const { data } = context.getImageData(0, 0, size, size);

  const buckets = new Map<number, { count: number; sum: RGB }>();
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 200) continue; // transparent background
    const key = ((data[i] >> 4) << 8) | ((data[i + 1] >> 4) << 4) | (data[i + 2] >> 4);
    const bucket = buckets.get(key) ?? { count: 0, sum: [0, 0, 0] };
    bucket.count += 1;
    for (let c = 0; c < 3; c++) bucket.sum[c] += data[i + c];
    buckets.set(key, bucket);
  }
  const colours = [...buckets.values()]
    .sort((a, b) => b.count - a.count)
    .map(({ count, sum }) => hex(sum.map((v) => v / count) as RGB));

  const primary = colours.find((colour) => {
    const [saturation, lightness] = saturationAndLightness(colour);
    return saturation >= 0.35 && lightness >= 0.2 && lightness <= 0.7;
  });
  const dark = colours.find((colour) => colour !== primary && luminance(colour) < 0.05);
  return { primary, secondary: dark ?? (primary && mix(primary, "#000000", 0.3)) };
}
