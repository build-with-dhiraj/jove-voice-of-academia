import type { ElementType, ReactNode } from "react";

import { cn } from "@/lib/utils";
import type { ThemeSentiment } from "@/lib/types";

/**
 * Sentiment-tinted gradient text primitive.
 *
 * Renders its children with a horizontal CSS gradient clipped to the glyph
 * shapes via `background-clip: text`. Used for the FrequencyTable bucket
 * headers ("What academia values / debates / dislikes about JoVE") so the
 * three sentiment surfaces read as distinct hues at a glance.
 *
 * Design intent:
 * - **Pure CSS** — no JS animation, no GSAP, no canvas. Static gradient
 *   (unlike the reactbits.dev source which animates background-position
 *   via motion/react). Static fits the v1.2 ambient: the Antigravity hero
 *   already supplies motion; bucket headers should be calm dividers.
 * - **<1KB bundle delta** — this component is ~40 lines plus three CSS
 *   custom properties in globals.css.
 * - **Accessible** — gradient stops chosen to maintain ≥4.5:1 contrast
 *   against page background on every stop (see globals.css comment block).
 *
 * Gradient tokens live in `app/globals.css` as
 * `--sentiment-{positive,neutral,negative}-grad` so any future component
 * (ambient backgrounds, hover glows, sparklines) can reuse the same hues
 * without forking the palette.
 */

const SENTIMENT_GRADIENTS: Record<ThemeSentiment, string> = {
  positive: "var(--sentiment-positive-grad)",
  neutral: "var(--sentiment-neutral-grad)",
  negative: "var(--sentiment-negative-grad)",
};

interface GradientTextProps {
  /** Which sentiment bucket this header belongs to. Picks the gradient. */
  sentiment: ThemeSentiment;
  /** The visible text. Glyph shapes clip the gradient via background-clip. */
  children: ReactNode;
  /**
   * Element to render. Defaults to `span` so the component is layout-neutral
   * — callers pick whether the gradient is wrapped in an `h2`, a `div`, etc.
   * Render it AS a heading by passing `as="h2"` rather than wrapping.
   */
  as?: ElementType;
  className?: string;
}

export function GradientText({
  sentiment,
  children,
  as: Component = "span",
  className,
}: GradientTextProps) {
  return (
    <Component
      className={cn(
        // background-clip:text + transparent fill is what produces the
        // "gradient inside glyph shapes" effect. The bg-clip-text Tailwind
        // utility sets both `background-clip: text` and the WebKit prefix.
        "inline-block bg-clip-text text-transparent",
        // Force the gradient to fill the box even on multi-line wraps —
        // without this, very short headlines look washed out at the edges
        // because the linear-gradient defaults to the text box width.
        "[background-size:100%_100%]",
        className,
      )}
      style={{ backgroundImage: SENTIMENT_GRADIENTS[sentiment] }}
    >
      {children}
    </Component>
  );
}
