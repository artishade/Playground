# Theme Design Specification — Agent_Linux Console

## Core Aesthetic: Pitch Black Dark Mode
All themes follow the **pitch black background (#000000)** + **minimalist AI chat interface** design language, with each theme differentiated by its **accent color** and **subtle tonal layering**.

## Theme Palette Architecture

Each theme provides these CSS custom properties:
```
--bg:      Main background (pitch black: #000000)
--bg-2:    Secondary layer (rail, sidebar)  
--bg-3:    Tertiary layer (panels, composer)
--panel:   Panel surface (cards, messages) — translucent
--panel-2: Hover/focus panel state
--line:    Border dividers
--line-2:  Stronger border emphasis
--fg:      Primary text
--dim:     Secondary text
--dim-2:   Muted/disabled text
--acc:     Primary accent (buttons, links, highlights)
--acc2:    Secondary accent (code, labels)
--acc-soft: Subtle accent background
--acc-line: Accent border
--ok:      Success
--warn:    Warning
--err:     Error
--term-bg: Terminal background
--term-fg: Terminal foreground
```

## Applied Theme Designs

### 1. NOVA (Default — Pure Pitch Black)
**Mood:** Clean, focused, distraction-free  
**Accent:** Electric Blue (#3b82f6)  
**Secondary:** Cyan (#06b6d4)

```
--bg:       #000000
--bg-2:     #0a0a0b
--bg-3:     #121214
--panel:    rgba(255,255,255,.03)
--panel-2:  rgba(255,255,255,.06)
--line:     rgba(255,255,255,.06)
--line-2:   rgba(255,255,255,.14)
--fg:       #f0f0f2
--dim:      #a0a0ab
--dim-2:    #6e6e7a
--acc:      #3b82f6
--acc2:     #06b6d4
--acc-soft: rgba(59,130,246,.14)
--acc-line: rgba(59,130,246,.38)
--ok:       #34c77b
--warn:     #f2a33c
--err:      #ef5e6e
--term-bg:  #000000
--term-fg:  #cdd2e0
```

### 2. CHERRY (Classic — Red Accented)
**Mood:** Warm, bold, energetic  
**Accent:** Crimson (#eb5757)  
**Secondary:** Rose (#e05e6f)

```
--bg:       #000000
--bg-2:     #0d0d12
--bg-3:     #121218
--panel:    rgba(255,255,255,.03)
--panel-2:  rgba(255,255,255,.06)
--line:     rgba(255,255,255,.07)
--line-2:   rgba(255,255,255,.15)
--fg:       #eceef4
--dim:      #9aa0b2
--dim-2:    #626879
--acc:      #eb5757
--acc2:     #e05e6f
--acc-soft: rgba(235,87,87,.14)
--acc-line: rgba(235,87,87,.38)
--ok:       #34c77b
--warn:     #f2a33c
--err:      #ef5e6e
--term-bg:  #000000
--term-fg:  #cdd2e0
```

### 3. OBSIDIAN (Deep Purple)
**Mood:** Mystical, deep, focused  
**Accent:** Royal Purple (#7c5cff)  
**Secondary:** Electric Cyan (#22d3ee)

```
--bg:       #000000
--bg-2:     #0a0a0f
--bg-3:     #10101c
--panel:    rgba(255,255,255,.03)
--panel-2:  rgba(255,255,255,.055)
--line:     rgba(255,255,255,.075)
--line-2:   rgba(255,255,255,.16)
--fg:       #e9edf8
--dim:      #8d95ab
--dim-2:    #5d6579
--acc:      #7c5cff
--acc2:     #22d3ee
--acc-soft: rgba(124,92,255,.14)
--acc-line: rgba(124,92,255,.38)
--ok:       #34c77b
--warn:     #f2a33c
--err:      #ef5e6e
--term-bg:  #000000
--term-fg:  #cbd5e1
```

### 4. PLASMA (Pink-Purple Gradient)
**Mood:** Futuristic, vibrant, energetic  
**Accent:** Hot Pink (#ff3ea5)  
**Secondary:** Violet (#8b5cf6)

```
--bg:       #000000
--bg-2:     #0a0510
--bg-3:     #1a0a2a
--panel:    rgba(255,255,255,.03)
--panel-2:  rgba(255,255,255,.055)
--line:     rgba(255,255,255,.075)
--line-2:   rgba(255,255,255,.16)
--fg:       #e9edf8
--dim:      #8d95ab
--dim-2:    #5d6579
--acc:      #ff3ea5
--acc2:     #8b5cf6
--acc-soft: rgba(255,62,165,.14)
--acc-line: rgba(255,62,165,.38)
--ok:       #34c77b
--warn:     #f2a33c
--err:      #ef5e6e
--term-bg:  #000000
--term-fg:  #e6d9f5
```

### 5. MATRIX (Neon Green)
**Mood:** Cyberpunk, terminal, hacker  
**Accent:** Neon Green (#3ee07f)  
**Secondary:** Lime (#a3e635)

```
--bg:       #000000
--bg-2:     #040a07
--bg-3:     #0b1a14
--panel:    rgba(255,255,255,.03)
--panel-2:  rgba(255,255,255,.055)
--line:     rgba(255,255,255,.075)
--line-2:   rgba(255,255,255,.16)
--fg:       #e9edf8
--dim:      #8d95ab
--dim-2:    #5d6579
--acc:      #3ee07f
--acc2:     #a3e635
--acc-soft: rgba(62,224,127,.14)
--acc-line: rgba(62,224,127,.38)
--ok:       #34c77b
--warn:     #f2a33c
--err:      #ef5e6e
--term-bg:  #000000
--term-fg:  #c8f7d6
```

### 6. GLACIER (Arctic Blue)
**Mood:** Cool, calm, professional  
**Accent:** Ice Blue (#38bdf8)  
**Secondary:** Periwinkle (#818cf8)

```
--bg:       #000000
--bg-2:     #040810
--bg-3:     #0a1830
--panel:    rgba(255,255,255,.03)
--panel-2:  rgba(255,255,255,.055)
--line:     rgba(255,255,255,.075)
--line-2:   rgba(255,255,255,.16)
--fg:       #e9edf8
--dim:      #8d95ab
--dim-2:    #5d6579
--acc:      #38bdf8
--acc2:     #818cf8
--acc-soft: rgba(56,189,248,.14)
--acc-line: rgba(56,189,248,.38)
--ok:       #34c77b
--warn:     #f2a33c
--err:      #ef5e6e
--term-bg:  #000000
--term-fg:  #cfe3ff
```

### 7. EMBER (Warm Orange-Red)
**Mood:** Warm, intense, fiery  
**Accent:** Burnt Orange (#fb923c)  
**Secondary:** Coral (#f43f5e)

```
--bg:       #000000
--bg-2:     #0c0705
--bg-3:     #1c0f0a
--panel:    rgba(255,255,255,.03)
--panel-2:  rgba(255,255,255,.055)
--line:     rgba(255,255,255,.075)
--line-2:   rgba(255,255,255,.16)
--fg:       #e9edf8
--dim:      #8d95ab
--dim-2:    #5d6579
--acc:      #fb923c
--acc2:     #f43f5e
--acc-soft: rgba(251,146,60,.14)
--acc-line: rgba(251,146,60,.38)
--ok:       #34c77b
--warn:     #f2a33c
--err:      #ef5e6e
--term-bg:  #000000
--term-fg:  #f6ddd0
```

## Cross-Function Theme Consistency

### Top Header (Rail + Pill Badges)
- Rail background uses `--bg-2` (secondary layer)
- Icons use `--dim` when inactive, `--fg` when active
- Active state: `--acc-soft` background, `--acc-line` border
- Pills/badges use `--acc` for accent dots, `--panel` surface

### Hero Content (Centered Logo + Title)
- AgentX logo on pitch-black background with accent glow
- Title text uses `--fg` for primary, `--dim` for subtitle
- Gradient logo uses `--acc` to `--acc2`

### Bottom Input Bar (Composer)
- Container: `--bg-3` with rounded corners (--radius: 14px)
- Text area placeholder: `--dim-2`
- Controls: `--panel` surface, `--dim` icons, `--acc-line` focus
- Action button: `--acc` filled with upward arrow, hover: brightness(1.07)

### Footer Notice
- Text: `--dim-2`, font: 10px var(--mono)
- "AI-generated content, for Experiment only"

## Implementation Notes

1. All themes share `--bg: #000000` (true pitch black)
2. Layering uses opacity-based translucency (rgba) for depth
3. Terminal themes match each theme's accent for cursor
4. Theme persistence via localStorage (`agent_linux_theme`)
5. Theme switching updates: CSS variables, meta theme-color, terminal theme, settings dropdown