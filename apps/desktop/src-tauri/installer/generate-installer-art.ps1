# Generates the Windows installer artwork from the Nesqual design system.
#
# Reproducible on purpose: the BMPs are build artifacts, not hand-drawn assets,
# so regenerating after a brand change is one command rather than a design task.
#
#   powershell -ExecutionPolicy Bypass -File generate-installer-art.ps1
#
# Sizes are fixed by the installer frameworks, not by us:
#   NSIS header       150 x  57   top-right strip on interior pages
#   NSIS sidebar      164 x 314   left panel on the welcome / finish pages
#   WiX  banner       493 x  58   top strip on most dialogs
#   WiX  dialog       493 x 312   full-bleed art on the welcome/exit dialogs
# All must be 24-bit BMP. PNG or 32-bit BMP silently fails to render.
#
# Why vector and not `packages/ui/assets/nesqual-logo.png`
# -------------------------------------------------------
# The PNG is the full 707x353 lockup (mark + "ESQUAL" + tagline). Squeezed into
# a 150x57 header the wordmark is four pixels tall and the diagonal turns to
# mush - which is exactly what the previous version of this script produced.
# `packages/ui/src/logo.ts` carries a traced vector of the mark, measured to
# within one pixel of the bitmap, so we draw that instead and it is crisp at
# any size. The mark alone is the right lockup here anyway: the page beside it
# already says "Nesq Bot" in type.
#
# Nothing is hardcoded that the design system already owns - the geometry and
# both brand colours are parsed out of packages/ui at generation time, so this
# script cannot drift from the source of truth without failing loudly.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"   # a half-drawn banner must fail the build,
                                  # not ship as a blank navy rectangle

Add-Type -AssemblyName System.Drawing

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ui = Join-Path $here "..\..\..\..\packages\ui\src"

# Resolve-Path's PathInfo stringifies a UNC location as
# "Microsoft.PowerShell.Core\FileSystem::\\server\share\..." and .NET rejects
# that provider-qualified form outright, so always take .ProviderPath.
function Read-UiSource([string]$name) {
    $path = Join-Path $ui $name
    if (-not (Test-Path $path)) { throw "$name not found at $path" }
    [IO.File]::ReadAllText((Resolve-Path $path).ProviderPath)
}

$logoSrc = Read-UiSource "logo.ts"
$brandSrc = Read-UiSource "brand.ts"
$tokensSrc = Read-UiSource "tokens.ts"

function Get-Match([string]$text, [string]$pattern, [string]$what) {
    $m = [regex]::Match($text, $pattern)
    if (-not $m.Success) { throw "could not read $what out of packages/ui - has the source moved?" }
    $m.Groups[1].Value
}

function ConvertTo-Color([string]$hex) {
    [System.Drawing.ColorTranslator]::FromHtml($hex)
}

# ------------------------------------------------------------------ #
# Brand, straight out of the design system
# ------------------------------------------------------------------ #

$markHex = Get-Match $brandSrc 'BRAND_MARK_HEX\s*=\s*"(#[0-9a-fA-F]{6})"' "the mark colour"
$inkHex = Get-Match $brandSrc 'BRAND_INK_HEX\s*=\s*"(#[0-9a-fA-F]{6})"' "the ink colour"
$navyHex = Get-Match $tokensSrc 'brandNavy\s*=\s*"(#[0-9a-fA-F]{6})"' "the brand navy"

$accent = ConvertTo-Color $markHex
$ink = ConvertTo-Color $inkHex
$navy = ConvertTo-Color $navyHex
$surface = ConvertTo-Color (Get-Match $tokensSrc 'surface:\s*"(#[0-9a-fA-F]{6})"' "the dark surface colour")

# Two navy tints for the structural lines. Deliberately near-invisible: they
# give the panel a texture instead of leaving the mark floating on a void, and
# they must not compete with the single accent.
$hatch = ConvertTo-Color "#12162e"
$edge = ConvertTo-Color "#1c2340"

# ------------------------------------------------------------------ #
# Geometry, also straight out of the design system
# ------------------------------------------------------------------ #

$markW = [double](Get-Match $logoSrc 'NESQUAL_MARK_WIDTH\s*=\s*(\d+)' "the mark width")
$markH = [double](Get-Match $logoSrc 'NESQUAL_MARK_HEIGHT\s*=\s*(\d+)' "the mark height")
$pathInk = Get-Match $logoSrc 'ink:\s*"([^"]+)"' "the mark's ink path"
$pathAccent = Get-Match $logoSrc 'accent:\s*"([^"]+)"' "the mark's accent path"

# The two paths are closed polygons of absolute moveto/lineto only - see the
# trace notes in logo.ts. Anything else means the artwork gained a curve and
# this parser needs to grow, so refuse rather than draw a mangled mark.
function ConvertTo-Polygon([string]$d) {
    if ($d -notmatch '^[MLZ0-9 .,\-]+$') { throw "unsupported path command in '$d'" }
    $pts = New-Object System.Collections.Generic.List[System.Drawing.PointF]
    foreach ($m in [regex]::Matches($d, '[ML]\s*(-?[\d.]+)[\s,]+(-?[\d.]+)')) {
        $pts.Add((New-Object System.Drawing.PointF([single]$m.Groups[1].Value, [single]$m.Groups[2].Value)))
    }
    if ($pts.Count -lt 3) { throw "path '$d' parsed to $($pts.Count) points" }
    , $pts.ToArray()
}

$polyInk = ConvertTo-Polygon $pathInk
$polyAccent = ConvertTo-Polygon $pathAccent

# Draw the mark with its top-left at ($x,$y) and the given height in px.
function Add-Mark {
    param([System.Drawing.Graphics]$G, [double]$X, [double]$Y, [double]$Height)

    $scale = $Height / $markH
    $state = $G.Save()
    $G.TranslateTransform([single]$X, [single]$Y)
    $G.ScaleTransform([single]$scale, [single]$scale)
    $brushInk = New-Object System.Drawing.SolidBrush($ink)
    $brushAccent = New-Object System.Drawing.SolidBrush($accent)
    $G.FillPolygon($brushInk, $polyInk)
    $G.FillPolygon($brushAccent, $polyAccent)
    $brushInk.Dispose()
    $brushAccent.Dispose()
    $G.Restore($state)
}

# The hatch runs at the mark's own slope: every diagonal in the artwork shares
# dx/dy = 1.75, so the texture is the mark's geometry repeated, not decoration
# borrowed from somewhere else.
$markSlope = 1.75

function Add-Hatch {
    param([System.Drawing.Graphics]$G, [int]$Width, [int]$Height, [int]$Spacing)

    $pen = New-Object System.Drawing.Pen($hatch, 1.0)
    $run = $Height * $markSlope
    # Down-and-right, the direction the mark's own diagonals travel. Start far
    # enough left that the first line still crosses the bottom edge.
    for ($x = [int](-$run); $x -lt $Width; $x += $Spacing) {
        $G.DrawLine($pen, [single]$x, 0.0, [single]($x + $run), [single]$Height)
    }
    $pen.Dispose()
}

# ------------------------------------------------------------------ #
# Canvases
# ------------------------------------------------------------------ #

function New-Canvas([int]$Width, [int]$Height, [System.Drawing.Color]$Ground) {
    $bmp = New-Object System.Drawing.Bitmap($Width, $Height, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    # Flat ground. A gradient banks badly at 24-bit across these short runs, so
    # the depth comes from the hatch and the one accent rule instead.
    $bg = New-Object System.Drawing.SolidBrush($Ground)
    $g.FillRectangle($bg, 0, 0, $Width, $Height)
    $bg.Dispose()
    , @($bmp, $g)
}

function Save-Canvas($bmp, $g, [string]$Out, [int]$Width, [int]$Height) {
    $g.Dispose()
    $bmp.Save((Join-Path $here $Out), [System.Drawing.Imaging.ImageFormat]::Bmp)
    $bmp.Dispose()
    "{0,-22} {1}x{2}" -f $Out, $Width, $Height
}

function Add-Rule {
    param([System.Drawing.Graphics]$G, [double]$X, [double]$Y, [double]$W, [double]$H, [System.Drawing.Color]$Color)
    $b = New-Object System.Drawing.SolidBrush($Color)
    $G.FillRectangle($b, [single]$X, [single]$Y, [single]$W, [single]$H)
    $b.Dispose()
}

# --- Tall panel: NSIS welcome/finish sidebar, WiX welcome/exit dialog -------
#
# The NSIS page behind this bitmap is painted the same navy, so without a
# deliberate edge the panel would end in a seam nobody chose. The right-hand
# rule is that edge: a dark divider the full height with one accent segment
# level with the mark.
function New-TallPanel {
    param([int]$Width, [int]$Height, [string]$Out, [double]$MarkHeight)

    $c = New-Canvas $Width $Height $navy
    $bmp = $c[0]; $g = $c[1]

    Add-Hatch -G $g -Width $Width -Height $Height -Spacing 22

    $mh = $MarkHeight
    $mw = $mh * ($markW / $markH)
    $mx = [Math]::Round(($Width - $mw) / 2)
    $my = [Math]::Round($Height * 0.30)
    Add-Mark -G $g -X $mx -Y $my -Height $mh

    # One accent rule under the mark, at the mark's own width fraction.
    $ruleW = [Math]::Round($mw * 0.5)
    Add-Rule -G $g -X ([Math]::Round(($Width - $ruleW) / 2)) -Y ($my + $mh + [Math]::Round($Height * 0.075)) -W $ruleW -H 2 -Color $accent

    Add-Rule -G $g -X ($Width - 1) -Y 0 -W 1 -H $Height -Color $edge
    $segH = [Math]::Round($mh * 0.9)
    Add-Rule -G $g -X ($Width - 1) -Y ($my + ($mh - $segH) / 2) -W 1 -H $segH -Color $accent

    Save-Canvas $bmp $g $Out $Width $Height
}

# --- Header strip: NSIS interior pages, WiX banner -------------------------
#
# Just the mark, right-aligned. At 57 px tall there is no room for a second
# element that would still be legible.
#
# The ground is `surface`, not `bg`: the installer paints the header band one
# step lighter than the page so that the two read apart without a separator
# line, and this bitmap has to sit flush inside that band. Change one and the
# other has to follow - see NESQ_GUIINIT in installer.nsi.
function New-HeaderStrip {
    param([int]$Width, [int]$Height, [string]$Out, [double]$MarkHeight, [int]$Margin)

    $c = New-Canvas $Width $Height $surface
    $bmp = $c[0]; $g = $c[1]

    $mh = $MarkHeight
    $mw = $mh * ($markW / $markH)
    $mx = $Width - $mw - $Margin
    $my = ($Height - $mh) / 2
    Add-Mark -G $g -X $mx -Y $my -Height $mh

    Save-Canvas $bmp $g $Out $Width $Height
}

New-HeaderStrip -Width 150 -Height  57 -Out "nsis-header.bmp"  -MarkHeight 34 -Margin 20
New-TallPanel   -Width 164 -Height 314 -Out "nsis-sidebar.bmp" -MarkHeight 96
New-HeaderStrip -Width 493 -Height  58 -Out "wix-banner.bmp"   -MarkHeight 34 -Margin 26
New-TallPanel   -Width 493 -Height 312 -Out "wix-dialog.bmp"   -MarkHeight 110

"done"
