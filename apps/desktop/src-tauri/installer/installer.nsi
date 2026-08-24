; Nesq Bot NSIS installer.
;
; This is a fork of Tauri's stock `installer.nsi` (tauri-bundler, cli v2.11.4),
; wired up through `bundle.windows.nsis.template` in tauri.conf.json. It is
; still a handlebars template: Tauri renders the double-brace placeholders
; before handing it to makensis, so every one of them has to survive edits
; intact - and a stray pair of braces anywhere, comments included, is a build
; failure rather than a comment.
;
; What is deliberately *unchanged* from upstream, because it is load-bearing:
; WebView2 detection and bootstrapping, perUser/perMachine/both install modes,
; upgrade + downgrade detection and the WiX-migration path, uninstall
; registration, kill-app-before-upgrade, file associations, deep links,
; installer hooks, and the language machinery. Rewriting from scratch would
; have dropped all of it.
;
; What is ours, and why
; ---------------------
;   1. Dark theme. `#0b0d1a` ground and `#8499da` accent, the same palette the
;      app itself uses (packages/ui/src/tokens.ts). MUI2 hands us MUI_BGCOLOR /
;      MUI_TEXTCOLOR for the header and the full-window pages; everything else
;      is SetCtlColors per control, which is the only mechanism NSIS has.
;   2. Three pages instead of five. There is no licence page (internal tool, no
;      EULA to click through), no component tree (there is one component), and
;      no separate directory page - the install location moved onto the first
;      page as a line of text plus a Change link, which is where a modern
;      installer puts it.
;   3. Real copy. "Nesq Bot", not "Welcome to the Nesq Bot Setup Wizard".
;
; Known limits, so nobody re-discovers them
; -----------------------------------------
;   * Push buttons (Back / Install / Cancel, Show details) are drawn by the
;     theme engine and ignore WM_CTLCOLORBTN, so SetCtlColors cannot darken
;     them. We opt the process into the OS dark mode instead, which does work
;     on Windows 10 1809+ and is a no-op on anything older - so on an older OS
;     the buttons stay light. That is a graceful degradation, not a break.
;   * A themed check box or radio button draws its own caption and sometimes
;     ignores the colour SetCtlColors gives it, which on a dark page means
;     black text nobody can read. It happens on the reinstall page and not on
;     the welcome, finish or uninstall-confirm pages, for no reason we could
;     isolate - see the long note in PageReinstall. Anywhere it bites, put the
;     caption in a STATIC beside a caption-less button.
;   * The bitmaps are exact-size 24-bit BMPs (150x57, 164x314). MUI stretches
;     the sidebar to the dialog-unit size of the control, so on a high-DPI
;     display it is scaled up and the hatch softens. Authoring at 2x would make
;     the 100% case worse, since NSIS downscales without interpolation.
;   * The three separator lines are collapsed rather than restyled, and the
;     header is a lighter band instead - see NESQ_COLLAPSE.
;   * Fonts are set control by control, never with SetFont. SetFont changes the
;     dialog base unit, and every MUI dimension - including the 109x193 units
;     the 164x314 sidebar bitmap has to fill exactly - is measured in them.

Unicode true
ManifestDPIAware true
; Add in `dpiAwareness` `PerMonitorV2` to manifest for Windows 10 1607+ (note this should not affect lower versions since they should be able to ignore this and pick up `dpiAware` `true` set by `ManifestDPIAware true`)
; Currently undocumented on NSIS's website but is in the Docs folder of source tree, see
; https://github.com/kichik/nsis/blob/5fc0b87b819a9eec006df4967d08e522ddd651c9/Docs/src/attributes.but#L286-L300
; https://github.com/tauri-apps/tauri/pull/10106
ManifestDPIAwareness PerMonitorV2

!if "{{compression}}" == "none"
  SetCompress off
!else
  ; Set the compression algorithm. We default to LZMA.
  SetCompressor /SOLID "{{compression}}"
!endif

; Keep above !include to stay ahead of any plugin command
; see https://github.com/tauri-apps/tauri/pull/15422#discussion_r3289239624
{{#if signed_plugins_path}}
!addplugindir "{{signed_plugins_path}}"
{{/if}}

!include MUI2.nsh
!include FileFunc.nsh
!include x64.nsh
!include WordFunc.nsh
!include "utils.nsh"
!include "FileAssociation.nsh"
!include "Win\COM.nsh"
!include "Win\Propkey.nsh"
!include "StrFunc.nsh"
${StrCase}
${StrLoc}

{{#if installer_hooks}}
!include "{{installer_hooks}}"
{{/if}}

!define WEBVIEW2APPGUID "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

!define MANUFACTURER "{{manufacturer}}"
!define PRODUCTNAME "{{product_name}}"
!define VERSION "{{version}}"
!define VERSIONWITHBUILD "{{version_with_build}}"
!define HOMEPAGE "{{homepage}}"
!define INSTALLMODE "{{install_mode}}"
!define LICENSE "{{license}}"
!define INSTALLERICON "{{installer_icon}}"
!define SIDEBARIMAGE "{{sidebar_image}}"
!define HEADERIMAGE "{{header_image}}"
!define UNINSTALLERICON "{{uninstaller_icon}}"
!define UNINSTALLERHEADERIMAGE "{{uninstaller_header_image}}"
!define MAINBINARYNAME "{{main_binary_name}}"
!define MAINBINARYSRCPATH "{{main_binary_path}}"
!define BUNDLEID "{{bundle_id}}"
!define COPYRIGHT "{{copyright}}"
!define OUTFILE "{{out_file}}"
!define ARCH "{{arch}}"
!define ADDITIONALPLUGINSPATH "{{additional_plugins_path}}"
!define ALLOWDOWNGRADES "{{allow_downgrades}}"
!define DISPLAYLANGUAGESELECTOR "{{display_language_selector}}"
!define INSTALLWEBVIEW2MODE "{{install_webview2_mode}}"
!define WEBVIEW2INSTALLERARGS "{{webview2_installer_args}}"
!define WEBVIEW2BOOTSTRAPPERPATH "{{webview2_bootstrapper_path}}"
!define WEBVIEW2INSTALLERPATH "{{webview2_installer_path}}"
!define MINIMUMWEBVIEW2VERSION "{{minimum_webview2_version}}"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCTNAME}"
!define MANUKEY "Software\${MANUFACTURER}"
!define MANUPRODUCTKEY "${MANUKEY}\${PRODUCTNAME}"
!define UNINSTALLERSIGNCOMMAND "{{uninstaller_sign_cmd}}"
!define ESTIMATEDSIZE "{{estimated_size}}"
!define STARTMENUFOLDER "{{start_menu_folder}}"

;--------------------------------
; Nesqual palette
;
; Hex is RRGGBB for SetCtlColors / MUI. Values are the app's own dark palette
; from packages/ui/src/tokens.ts - bg, surface, text, textMuted, border and the
; sampled mark colour - so the installer and the window it installs are the
; same two colours plus one accent.

!define NESQ_BG      "0B0D1A"
!define NESQ_SURFACE "12152A"
!define NESQ_TEXT    "F2F4FF"
!define NESQ_MUTED   "9AA3C7"
!define NESQ_DIM     "858EB0"
!define NESQ_ACCENT  "8499DA"
!define NESQ_BORDER  "2A3050"

; COLORREF is 0x00BBGGRR, the other way round from the strings above. Only the
; progress bar needs it, because PBM_SETBARCOLOR is a Win32 message.
!define NESQ_ACCENT_REF 0x00DA9984
!define NESQ_SURFACE_REF 0x002A1512

; One line about the product, for the first page. Kept in sync by hand with
; `longDescription` in tauri.conf.json - the bundler does not expose it to the
; NSIS template, only to the MSI.
!define NESQ_STRAPLINE "Always-on AI teammates, each with an isolated Linux desktop."

!define /ifndef SS_PATHELLIPSIS 0x00008000
!define /ifndef PBM_SETBARCOLOR 0x0409
!define /ifndef PBM_SETBKCOLOR  0x2001

Var PassiveMode
Var UpdateMode
Var NoShortcutMode
Var WixMode
Var OldMainBinaryName

Var NesqFontBody
Var NesqFontSmall
Var NesqFontCaps
Var NesqFontTitle
Var NesqDesktopShortcut
Var NesqPathLabel
Var NesqOption1
Var NesqOption2

Name "${PRODUCTNAME}"
BrandingText "${COPYRIGHT}"
OutFile "${OUTFILE}"

; We don't actually use this value as default install path,
; it's just for nsis to append the product name folder in the directory selector
; https://nsis.sourceforge.io/Reference/InstallDir
!define PLACEHOLDER_INSTALL_DIR "placeholder\${PRODUCTNAME}"
InstallDir "${PLACEHOLDER_INSTALL_DIR}"

VIProductVersion "${VERSIONWITHBUILD}"
VIAddVersionKey "ProductName" "${PRODUCTNAME}"
VIAddVersionKey "FileDescription" "${PRODUCTNAME} Setup"
VIAddVersionKey "CompanyName" "${MANUFACTURER}"
VIAddVersionKey "LegalCopyright" "${COPYRIGHT}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"

# additional plugins
!addplugindir "${ADDITIONALPLUGINSPATH}"

; Uninstaller signing command
!if "${UNINSTALLERSIGNCOMMAND}" != ""
  !uninstfinalize '${UNINSTALLERSIGNCOMMAND}'
!endif

; Handle install mode, `perUser`, `perMachine` or `both`
!if "${INSTALLMODE}" == "perMachine"
  RequestExecutionLevel admin
!endif

!if "${INSTALLMODE}" == "currentUser"
  RequestExecutionLevel user
!endif

!if "${INSTALLMODE}" == "both"
  !define MULTIUSER_MUI
  !define MULTIUSER_INSTALLMODE_INSTDIR "${PRODUCTNAME}"
  !define MULTIUSER_INSTALLMODE_COMMANDLINE
  !if "${ARCH}" == "x64"
    !define MULTIUSER_USE_PROGRAMFILES64
  !else if "${ARCH}" == "arm64"
    !define MULTIUSER_USE_PROGRAMFILES64
  !endif
  !define MULTIUSER_INSTALLMODE_DEFAULT_REGISTRY_KEY "${UNINSTKEY}"
  !define MULTIUSER_INSTALLMODE_DEFAULT_REGISTRY_VALUENAME "CurrentUser"
  !define MULTIUSER_INSTALLMODEPAGE_SHOWUSERNAME
  !define MULTIUSER_INSTALLMODE_FUNCTION RestorePreviousInstallLocation
  !define MULTIUSER_EXECUTIONLEVEL Highest
  !include MultiUser.nsh
!endif

; Installer icon
!if "${INSTALLERICON}" != ""
  !define MUI_ICON "${INSTALLERICON}"
!endif

; Installer sidebar image
!if "${SIDEBARIMAGE}" != ""
  !define MUI_WELCOMEFINISHPAGE_BITMAP "${SIDEBARIMAGE}"
  !define MUI_UNWELCOMEFINISHPAGE_BITMAP "${SIDEBARIMAGE}"
!endif

; Enable header images for installer and uninstaller pages when either image is configured.
!if "${HEADERIMAGE}" != ""
  !define MUI_HEADERIMAGE
!else if "${UNINSTALLERHEADERIMAGE}" != ""
  !define MUI_HEADERIMAGE
!endif

; Installer header image
!if "${HEADERIMAGE}" != ""
  !define MUI_HEADERIMAGE_BITMAP "${HEADERIMAGE}"
!endif

; Uninstaller header image
!if "${UNINSTALLERHEADERIMAGE}" != ""
  !define MUI_HEADERIMAGE_UNBITMAP "${UNINSTALLERHEADERIMAGE}"
!endif

; Uninstaller icon
!if "${UNINSTALLERICON}" != ""
  !define MUI_UNICON "${UNINSTALLERICON}"
!endif

; The mark sits on the right of the header, with the page title on the left.
; MUI's default is the other way round, which reads as a 2001 toolbar.
!define MUI_HEADERIMAGE_RIGHT

; Dark theme. MUI applies these to the header, the header text and the two
; full-window pages; the interior pages are handled by hand further down.
!define MUI_BGCOLOR "${NESQ_BG}"
!define MUI_TEXTCOLOR "${NESQ_TEXT}"

; The details log and the progress bar on the install page.
!define MUI_INSTFILESPAGE_COLORS "${NESQ_MUTED} ${NESQ_BG}"
!define MUI_INSTFILESPAGE_PROGRESSBAR "smooth"

; Must be defined before the first MUI_LANGUAGE, which is what pulls in
; MUI_FUNCTION_GUIINIT.
!define MUI_CUSTOMFUNCTION_GUIINIT NesqGuiInit
!define MUI_CUSTOMFUNCTION_UNGUIINIT un.NesqGuiInit

; Define registry key to store installer language
!define MUI_LANGDLL_REGISTRY_ROOT "HKCU"
!define MUI_LANGDLL_REGISTRY_KEY "${MANUPRODUCTKEY}"
!define MUI_LANGDLL_REGISTRY_VALUENAME "Installer Language"

;--------------------------------
; Theming helpers
;
; Macros rather than functions: the uninstaller is a separate executable and
; cannot call a function that is not `un.`-prefixed, so anything shared has to
; be textual.

; Standard push buttons, check boxes and radio buttons are drawn by uxtheme and
; ignore WM_CTLCOLORBTN, so SetCtlColors cannot touch them. Opting the process
; into dark mode does work, and is the same mechanism Explorer uses.
;
; Ordinal 135 is SetPreferredAppMode(PreferredAppMode) on Windows 10 1903+ and
; AllowDarkModeForApp(BOOL) on 1809; 2 means ForceDark to the first and simply
; "true" to the second, so the same call is right for both. It is undocumented
; and unnamed, hence the ordinal. On anything that does not export it - Windows
; 8.1, Server core, a future build that drops it - System::Call fails to
; resolve the symbol and does nothing at all. The visible consequence is light
; grey buttons on a dark page: worse looking, still completely functional.
!macro NESQ_ENABLE_DARKMODE
  System::Call 'uxtheme::#135(i 2)'
  System::Call 'uxtheme::#136()'
!macroend

; Per-control opt-in. Needed on top of the process-wide mode: a control only
; picks up the dark renderer once it is themed as one.
!macro NESQ_DARK_CONTROL HWND
  System::Call 'uxtheme::SetWindowTheme(p ${HWND}, w "DarkMode_Explorer", p 0)'
!macroend

; Text fields want a different class from buttons. DarkMode_Explorer leaves an
; edit control with its light 3D client edge, which on a dark page is a white
; box; DarkMode_CFD is the one that gives the flat dark border.
!macro NESQ_DARK_FIELD HWND
  System::Call 'uxtheme::SetWindowTheme(p ${HWND}, w "DarkMode_CFD", p 0)'
!macroend

; The title bar follows the window, not the process. Attribute 20 is
; DWMWA_USE_IMMERSIVE_DARK_MODE on Windows 10 2004 and later, 19 on the builds
; before it; whichever is wrong for the running build returns a failure code we
; ignore, and on Windows 8.1 dwmapi has the export but rejects both.
!macro NESQ_DARK_TITLEBAR HWND
  System::Call 'dwmapi::DwmSetWindowAttribute(p ${HWND}, i 20, *i 1, i 4)'
  System::Call 'dwmapi::DwmSetWindowAttribute(p ${HWND}, i 19, *i 1, i 4)'
!macroend

; NSIS's three separators are SS_ETCHEDHORZ statics - a COLOR_3DSHADOW hairline
; over a COLOR_3DHILIGHT one, which against navy is a grey scratch across the
; page. They cannot be recoloured: an etched static paints itself with DrawEdge
; and never asks the parent for a brush, so SetCtlColors has nothing to hook.
; Stripping SS_ETCHEDHORZ does stop the etching, but leaves a control painting
; in a fallback colour we do not control, which is worse than either outcome.
;
; So they are collapsed to nothing instead. Not hidden: MUI re-shows 1035 and
; 1045 on every transition between an interior and a full-window page, so a
; hide would last exactly one page. A zero-size window has nothing to paint and
; MUI's ShowWindow calls do not give it its size back.
;
; The structure they were providing is not lost - the header is a lighter band
; (see NESQ_GUIINIT) rather than a line, which is how the app draws its own
; chrome.
!macro NESQ_COLLAPSE HWND
  System::Call 'user32::MoveWindow(p ${HWND}, i 0, i 0, i 0, i 0, i 1)'
!macroend

; A themed progress bar draws itself in the system accent green and ignores
; PBM_SETBARCOLOR entirely. SetWindowTheme with a pair of blank strings takes
; the visual style off this one control, after which the classic renderer
; honours the colours. This is the documented way to un-theme a control.
!macro NESQ_PROGRESSBAR HWND
  System::Call 'uxtheme::SetWindowTheme(p ${HWND}, w " ", w " ")'
  SendMessage ${HWND} ${PBM_SETBKCOLOR} 0 ${NESQ_SURFACE_REF}
  SendMessage ${HWND} ${PBM_SETBARCOLOR} 0 ${NESQ_ACCENT_REF}
!macroend

; Everything the outer window owns: the frame, the button strip, the branding
; line and the separator above it. Shared by installer and uninstaller.
;
; `SetCtlColors $HWNDPARENT` colours the dialog background itself, which is the
; strip the buttons sit in. The buttons keep their own (themed) painting.
!macro NESQ_GUIINIT
  CreateFont $NesqFontBody "Segoe UI" "9" "400"
  CreateFont $NesqFontSmall "Segoe UI" "8" "400"
  CreateFont $NesqFontCaps "Segoe UI" "7" "600"
  CreateFont $NesqFontTitle "Segoe UI" "16" "600"

  !insertmacro NESQ_ENABLE_DARKMODE
  !insertmacro NESQ_DARK_TITLEBAR $HWNDPARENT

  SetCtlColors $HWNDPARENT "${NESQ_TEXT}" "${NESQ_BG}"

  ; MUI has already pointed these at the header controls and coloured them; we
  ; only restate the fonts, because MUI builds its bold header font out of
  ; $(^Font), which is still MS Shell Dlg.
  ;
  ; Why the font is set control by control instead of globally with SetFont:
  ; SetFont changes the dialog base unit, every dimension in NSIS and MUI is in
  ; dialog units, and the welcome bitmap is placed at 109x193 of them. Changing
  ; the font would silently resize that control away from the 164x314 the BMP
  ; actually is and leave it stretched. Per-control fonts change the glyphs
  ; without touching the geometry.
  CreateFont $0 "Segoe UI" "12" "600"
  SendMessage $mui.Header.Text ${WM_SETFONT} $0 0
  SendMessage $mui.Header.SubText ${WM_SETFONT} $NesqFontSmall 0

  ; The header is a band one step lighter than the page, which is what gives
  ; the interior pages their structure now that the separator lines are gone.
  ; MUI paints all three of these in MUI_BGCOLOR; this overrides it, and the
  ; header bitmap is generated on the same surface colour so it sits flush.
  SetCtlColors $mui.Header.Background "" "${NESQ_SURFACE}"
  SetCtlColors $mui.Header.Text "${NESQ_TEXT}" "${NESQ_SURFACE}"
  SetCtlColors $mui.Header.SubText "${NESQ_MUTED}" "${NESQ_SURFACE}"

  SetCtlColors $mui.Branding.Background "" "${NESQ_BG}"
  SetCtlColors $mui.Branding.Text "${NESQ_DIM}" "${NESQ_BG}"
  SendMessage $mui.Branding.Text ${WM_SETFONT} $NesqFontSmall 0

  ; Three separators, not two. 1035 sits above the button strip on interior
  ; pages and 1045 does the same on the full-window ones - MUI names both. The
  ; line under the header is 1036, and MUI keeps no variable for it, which is
  ; why it survived the first pass and stayed a grey scratch across the top of
  ; every interior page. 1036 only exists in the header UI variant, hence the
  ; guard.
  !insertmacro NESQ_COLLAPSE $mui.Line.Standard
  !insertmacro NESQ_COLLAPSE $mui.Line.FullWindow
  GetDlgItem $8 $HWNDPARENT 1036
  ${If} $8 <> 0
    !insertmacro NESQ_COLLAPSE $8
  ${EndIf}

  SendMessage $mui.Button.Next ${WM_SETFONT} $NesqFontBody 0
  SendMessage $mui.Button.Back ${WM_SETFONT} $NesqFontBody 0
  SendMessage $mui.Button.Cancel ${WM_SETFONT} $NesqFontBody 0
  !insertmacro NESQ_DARK_CONTROL $mui.Button.Next
  !insertmacro NESQ_DARK_CONTROL $mui.Button.Back
  !insertmacro NESQ_DARK_CONTROL $mui.Button.Cancel
!macroend

; The two functions that instantiate NESQ_GUIINIT live further down, below the
; page declarations: the `$mui.*` variables it touches are declared by MUI's
; own interface macro, which does not run until the first page is inserted, and
; NSIS resolves variables in one pass.

;--------------------------------
; Installer pages, must be ordered as they appear

; 1. Welcome page - product, version, where it is going, and one option.
;
; MUI's own body text is thrown away in the SHOW callback and replaced with our
; own controls. We keep MUI's page rather than writing a `Page custom` because
; it owns the full-window plumbing (hiding the header, swapping the separator,
; loading and stretching the sidebar bitmap) that a bare custom page does not.
!define MUI_WELCOMEPAGE_TITLE "${PRODUCTNAME}"
!define MUI_WELCOMEPAGE_TEXT ""
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
!define MUI_PAGE_CUSTOMFUNCTION_SHOW NesqWelcomeShow
!insertmacro MUI_PAGE_WELCOME

; 2. License Page (if defined)
!if "${LICENSE}" != ""
  !define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
  !insertmacro MUI_PAGE_LICENSE "${LICENSE}"
!endif

; 3. Install mode (if it is set to `both`)
!if "${INSTALLMODE}" == "both"
  !define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
  !insertmacro MULTIUSER_PAGE_INSTALLMODE
!endif

; 4. Custom page to ask user if he wants to reinstall/uninstall
;    only if a previous installation was detected
Var ReinstallPageCheck
Page custom PageReinstall PageLeaveReinstall
Function PageReinstall
  ; Uninstall previous WiX installation if exists.
  ;
  ; A WiX installer stores the installation info in registry
  ; using a UUID and so we have to loop through all keys under
  ; `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall`
  ; and check if `DisplayName` and `Publisher` keys match ${PRODUCTNAME} and ${MANUFACTURER}
  ;
  ; This has a potential issue that there maybe another installation that matches
  ; our ${PRODUCTNAME} and ${MANUFACTURER} but wasn't installed by our WiX installer,
  ; however, this should be fine since the user will have to confirm the uninstallation
  ; and they can chose to abort it if doesn't make sense.
  ;
  ; Still worth keeping now that we no longer ship an MSI: anyone who installed
  ; one of the earlier MSI builds needs this path to upgrade cleanly.
  StrCpy $0 0
  wix_loop:
    EnumRegKey $1 HKLM "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" $0
    StrCmp $1 "" wix_loop_done ; Exit loop if there is no more keys to loop on
    IntOp $0 $0 + 1
    ReadRegStr $R0 HKLM "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$1" "DisplayName"
    ReadRegStr $R1 HKLM "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$1" "Publisher"
    StrCmp "$R0$R1" "${PRODUCTNAME}${MANUFACTURER}" 0 wix_loop
    ReadRegStr $R0 HKLM "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$1" "UninstallString"
    ${StrCase} $R1 $R0 "L"
    ${StrLoc} $R0 $R1 "msiexec" ">"
    StrCmp $R0 0 0 wix_loop_done
    StrCpy $WixMode 1
    StrCpy $R6 "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$1"
    Goto compare_version
  wix_loop_done:

  ; Check if there is an existing installation, if not, abort the reinstall page
  ReadRegStr $R0 SHCTX "${UNINSTKEY}" ""
  ReadRegStr $R1 SHCTX "${UNINSTKEY}" "UninstallString"
  ${IfThen} "$R0$R1" == "" ${|} Abort ${|}

  ; Compare this installar version with the existing installation
  ; and modify the messages presented to the user accordingly
  compare_version:
  StrCpy $R4 "$(older)"
  ${If} $WixMode = 1
    ReadRegStr $R0 HKLM "$R6" "DisplayVersion"
  ${Else}
    ReadRegStr $R0 SHCTX "${UNINSTKEY}" "DisplayVersion"
  ${EndIf}
  ${IfThen} $R0 == "" ${|} StrCpy $R4 "$(unknown)" ${|}

  nsis_tauri_utils::SemverCompare "${VERSION}" $R0
  Pop $R0
  ; Reinstalling the same version
  ${If} $R0 = 0
    StrCpy $R1 "$(alreadyInstalledLong)"
    StrCpy $R2 "$(addOrReinstall)"
    StrCpy $R3 "$(uninstallApp)"
    !insertmacro MUI_HEADER_TEXT "$(alreadyInstalled)" "$(chooseMaintenanceOption)"
  ; Upgrading
  ${ElseIf} $R0 = 1
    StrCpy $R1 "$(olderOrUnknownVersionInstalled)"
    StrCpy $R2 "$(uninstallBeforeInstalling)"
    StrCpy $R3 "$(dontUninstall)"
    !insertmacro MUI_HEADER_TEXT "$(alreadyInstalled)" "$(choowHowToInstall)"
  ; Downgrading
  ${ElseIf} $R0 = -1
    StrCpy $R1 "$(newerVersionInstalled)"
    StrCpy $R2 "$(uninstallBeforeInstalling)"
    !if "${ALLOWDOWNGRADES}" == "true"
      StrCpy $R3 "$(dontUninstall)"
    !else
      StrCpy $R3 "$(dontUninstallDowngrade)"
    !endif
    !insertmacro MUI_HEADER_TEXT "$(alreadyInstalled)" "$(choowHowToInstall)"
  ${Else}
    Abort
  ${EndIf}

  ; Skip showing the page if passive
  ;
  ; Note that we don't call this earlier at the begining
  ; of this function because we need to populate some variables
  ; related to current installed version if detected and whether
  ; we are downgrading or not.
  ${If} $PassiveMode = 1
    Call PageLeaveReinstall
  ${Else}
    ; Stash the two option captions: $R2 and $R3 hold them on the way in and
    ; the control handles on the way out.
    StrCpy $NesqOption1 $R2
    StrCpy $NesqOption2 $R3

    nsDialogs::Create 1018
    Pop $R4
    SetCtlColors $R4 "${NESQ_TEXT}" "${NESQ_BG}"
    ${IfThen} $(^RTL) = 1 ${|} nsDialogs::SetRTL $(^RTL) ${|}

    ${NSD_CreateLabel} 0 0 100% 28u $R1
    Pop $R1
    SetCtlColors $R1 "${NESQ_MUTED}" "${NESQ_BG}"
    SendMessage $R1 ${WM_SETFONT} $NesqFontBody 0

    ; The radio buttons carry no text of their own, and the captions are
    ; separate labels beside them.
    ;
    ; This is not decoration, it is the only construction that works here. A
    ; themed BUTTON draws its own caption, and on this page it drew it in the
    ; light theme's near-black - unreadable - however it was coloured:
    ; SetCtlColors before the theme call, after it, and with dark mode turned
    ; off for the control all produced the same black text. What makes it
    ; specific to this page is not clear: the checkbox on the welcome page and
    ; the one the uninstaller adds to its confirm page are the same class with
    ; the same treatment and both come out white. Rather than ship a page whose
    ; text happens to be legible or not for a reason nobody can name, the
    ; caption moves to a STATIC, which honours SetCtlColors on every dialog in
    ; this installer. nsDialogs' labels already carry SS_NOTIFY, so they stay
    ; clickable and the hit target ends up bigger than the stock control's.
    ;
    ; The buttons keep their own SetCtlColors: the *background* half of it is
    ; honoured, and without it the glyph sits in a white square.
    ${NSD_CreateRadioButton} 8u 54u 12u 12u ""
    Pop $R2
    SetCtlColors $R2 "${NESQ_TEXT}" "${NESQ_BG}"
    !insertmacro NESQ_DARK_CONTROL $R2
    ${NSD_OnClick} $R2 PageReinstallUpdateSelection
    ${NSD_CreateLabel} 24u 55u -8u 11u "$NesqOption1"
    Pop $NesqOption1
    SetCtlColors $NesqOption1 "${NESQ_TEXT}" "${NESQ_BG}"
    SendMessage $NesqOption1 ${WM_SETFONT} $NesqFontBody 0
    ${NSD_OnClick} $NesqOption1 NesqReinstallPickFirst

    ${NSD_CreateRadioButton} 8u 74u 12u 12u ""
    Pop $R3
    SetCtlColors $R3 "${NESQ_TEXT}" "${NESQ_BG}"
    !insertmacro NESQ_DARK_CONTROL $R3
    ${NSD_OnClick} $R3 PageReinstallUpdateSelection
    ${NSD_CreateLabel} 24u 75u -8u 11u "$NesqOption2"
    Pop $NesqOption2
    SetCtlColors $NesqOption2 "${NESQ_TEXT}" "${NESQ_BG}"
    SendMessage $NesqOption2 ${WM_SETFONT} $NesqFontBody 0
    ${NSD_OnClick} $NesqOption2 NesqReinstallPickSecond

    ; Disable this radio button if downgrading and downgrades are disabled
    !if "${ALLOWDOWNGRADES}" == "false"
      ${If} $R0 = -1
        EnableWindow $R3 0
        SetCtlColors $NesqOption2 "${NESQ_DIM}" "${NESQ_BG}"
      ${EndIf}
    !endif

    ; Check the first radio button if this the first time
    ; we enter this page or if the second button wasn't
    ; selected the last time we were on this page
    ${If} $ReinstallPageCheck <> 2
      SendMessage $R2 ${BM_SETCHECK} ${BST_CHECKED} 0
    ${Else}
      SendMessage $R3 ${BM_SETCHECK} ${BST_CHECKED} 0
    ${EndIf}

    ${NSD_SetFocus} $R2
    nsDialogs::Show
  ${EndIf}
FunctionEnd
Function PageReinstallUpdateSelection
  ; nsDialogs pushes the control handle before calling us; drop it, the state
  ; we care about is read from $R2 either way.
  Pop $0
  ${NSD_GetState} $R2 $R1
  ${If} $R1 == ${BST_CHECKED}
    StrCpy $ReinstallPageCheck 1
  ${Else}
    StrCpy $ReinstallPageCheck 2
  ${EndIf}
FunctionEnd
; Clicking a caption selects its option. The two radios are BS_AUTORADIOBUTTON
; and would deselect each other if the click landed on them, but a BM_SETCHECK
; sent from here does not run that logic, so both are set explicitly.
Function NesqReinstallPickFirst
  Pop $0
  SendMessage $R3 ${BM_SETCHECK} ${BST_UNCHECKED} 0
  SendMessage $R2 ${BM_SETCHECK} ${BST_CHECKED} 0
  StrCpy $ReinstallPageCheck 1
FunctionEnd
Function NesqReinstallPickSecond
  Pop $0
  ; A disabled radio must stay unselected - this is the downgrade lockout.
  System::Call 'user32::IsWindowEnabled(p $R3) i .r0'
  ${If} $0 = 0
    Return
  ${EndIf}
  SendMessage $R2 ${BM_SETCHECK} ${BST_UNCHECKED} 0
  SendMessage $R3 ${BM_SETCHECK} ${BST_CHECKED} 0
  StrCpy $ReinstallPageCheck 2
FunctionEnd
Function PageLeaveReinstall
  ${NSD_GetState} $R2 $R1

  ; If migrating from Wix, always uninstall
  ${If} $WixMode = 1
    Goto reinst_uninstall
  ${EndIf}

  ; In update mode, always proceeds without uninstalling
  ${If} $UpdateMode = 1
    Goto reinst_done
  ${EndIf}

  ; $R0 holds whether same(0)/upgrading(1)/downgrading(-1) version
  ; $R1 holds the radio buttons state:
  ;   1 => first choice was selected
  ;   0 => second choice was selected
  ${If} $R0 = 0 ; Same version, proceed
    ${If} $R1 = 1              ; User chose to add/reinstall
      Goto reinst_done
    ${Else}                    ; User chose to uninstall
      Goto reinst_uninstall
    ${EndIf}
  ${ElseIf} $R0 = 1 ; Upgrading
    ${If} $R1 = 1              ; User chose to uninstall
      Goto reinst_uninstall
    ${Else}
      Goto reinst_done         ; User chose NOT to uninstall
    ${EndIf}
  ${ElseIf} $R0 = -1 ; Downgrading
    ${If} $R1 = 1              ; User chose to uninstall
      Goto reinst_uninstall
    ${Else}
      Goto reinst_done         ; User chose NOT to uninstall
    ${EndIf}
  ${EndIf}

  reinst_uninstall:
    HideWindow
    ClearErrors

    ${If} $WixMode = 1
      ReadRegStr $R1 HKLM "$R6" "UninstallString"
      ExecWait '$R1' $0
    ${Else}
      ReadRegStr $4 SHCTX "${MANUPRODUCTKEY}" ""
      ReadRegStr $R1 SHCTX "${UNINSTKEY}" "UninstallString"
      ${IfThen} $UpdateMode = 1 ${|} StrCpy $R1 "$R1 /UPDATE" ${|} ; append /UPDATE
      ${IfThen} $PassiveMode = 1 ${|} StrCpy $R1 "$R1 /P" ${|} ; append /P
      StrCpy $R1 "$R1 _?=$4" ; append uninstall directory
      ExecWait '$R1' $0
    ${EndIf}

    BringToFront

    ${IfThen} ${Errors} ${|} StrCpy $0 2 ${|} ; ExecWait failed, set fake exit code

    ${If} $0 <> 0
    ${OrIf} ${FileExists} "$INSTDIR\${MAINBINARYNAME}.exe"
      ; User cancelled wix uninstaller? return to select un/reinstall page
      ${If} $WixMode = 1
      ${AndIf} $0 = 1602
        Abort
      ${EndIf}

      ; User cancelled NSIS uninstaller? return to select un/reinstall page
      ${If} $0 = 1
        Abort
      ${EndIf}

      ; Other erros? show generic error message and return to select un/reinstall page
      MessageBox MB_ICONEXCLAMATION "$(unableToUninstall)"
      Abort
    ${EndIf}
  reinst_done:
FunctionEnd

; 5. Choose install directory page
;
; Deliberately absent. A whole wizard step whose only content is a path most
; people never change is exactly the 1998 chrome we were asked to lose; the
; path and a Change link live on the welcome page instead, and `/D=` on the
; command line still works because that is handled by NSIS itself.

; 6. Start menu shortcut page
Var AppStartMenuFolder
!if "${STARTMENUFOLDER}" != ""
  !define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
  !define MUI_STARTMENUPAGE_DEFAULTFOLDER "${STARTMENUFOLDER}"
!else
  !define MUI_PAGE_CUSTOMFUNCTION_PRE Skip
!endif
!insertmacro MUI_PAGE_STARTMENU Application $AppStartMenuFolder

; 7. Installation page
!define MUI_PAGE_CUSTOMFUNCTION_SHOW NesqInstFilesShow
!insertmacro MUI_PAGE_INSTFILES

; 8. Finish page
;
; Don't auto jump to finish page after installation page,
; because the installation page has useful info that can be used debug any issues with the installer.
!define MUI_FINISHPAGE_NOAUTOCLOSE
; Show run app after installation.
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Launch ${PRODUCTNAME}"
!define MUI_FINISHPAGE_RUN_FUNCTION RunMainBinary
!define MUI_FINISHPAGE_TITLE "${PRODUCTNAME} is installed"
!define MUI_FINISHPAGE_TEXT "$INSTDIR"
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive
!define MUI_PAGE_CUSTOMFUNCTION_SHOW NesqFinishShow
!insertmacro MUI_PAGE_FINISH

Function RunMainBinary
  nsis_tauri_utils::RunAsUser "$INSTDIR\${MAINBINARYNAME}.exe" ""
FunctionEnd

;--------------------------------
; Page callbacks

Function NesqGuiInit
  !insertmacro NESQ_GUIINIT
FunctionEnd

Function un.NesqGuiInit
  !insertmacro NESQ_GUIINIT
FunctionEnd

Function NesqWelcomeShow
  ; MUI's stock body label is a single 130u block of wrapped text. We need four
  ; different weights and two controls, so it goes away and we lay out our own.
  ShowWindow $mui.WelcomePage.Text ${SW_HIDE}

  SetCtlColors $mui.WelcomePage.Title "${NESQ_TEXT}" "${NESQ_BG}"
  SendMessage $mui.WelcomePage.Title ${WM_SETFONT} $NesqFontTitle 0

  ${NSD_CreateLabel} 120u 46u 195u 28u "${NESQ_STRAPLINE}"
  Pop $0
  SetCtlColors $0 "${NESQ_MUTED}" "${NESQ_BG}"
  SendMessage $0 ${WM_SETFONT} $NesqFontBody 0

  ${NSD_CreateLabel} 120u 80u 195u 9u "Version ${VERSION}   -   ${MANUFACTURER}"
  Pop $0
  SetCtlColors $0 "${NESQ_DIM}" "${NESQ_BG}"
  SendMessage $0 ${WM_SETFONT} $NesqFontSmall 0

  ; Hairline. A label with no text, painted with its background brush.
  ${NSD_CreateLabel} 120u 100u 195u 1u ""
  Pop $0
  SetCtlColors $0 "" "${NESQ_BORDER}"

  ${NSD_CreateLabel} 120u 112u 195u 8u "INSTALL LOCATION"
  Pop $0
  SetCtlColors $0 "${NESQ_MUTED}" "${NESQ_BG}"
  SendMessage $0 ${WM_SETFONT} $NesqFontCaps 0

  ${NSD_CreateLabel} 120u 124u 150u 11u "$INSTDIR"
  Pop $NesqPathLabel
  ; Ellipsise in the middle of the path rather than clipping the tail, so the
  ; drive and the leaf both stay readable when someone picks a deep folder.
  ${NSD_AddStyle} $NesqPathLabel ${SS_PATHELLIPSIS}
  SetCtlColors $NesqPathLabel "${NESQ_TEXT}" "${NESQ_BG}"
  SendMessage $NesqPathLabel ${WM_SETFONT} $NesqFontBody 0

  ${NSD_CreateLink} 277u 124u 38u 11u "Change"
  Pop $0
  SetCtlColors $0 "${NESQ_ACCENT}" "${NESQ_BG}"
  SendMessage $0 ${WM_SETFONT} $NesqFontBody 0
  ${NSD_OnClick} $0 NesqChooseFolder

  ${NSD_CreateCheckbox} 120u 150u 195u 12u "Create a desktop shortcut"
  Pop $0
  SetCtlColors $0 "${NESQ_TEXT}" "${NESQ_BG}"
  SendMessage $0 ${WM_SETFONT} $NesqFontBody 0
  !insertmacro NESQ_DARK_CONTROL $0
  ${NSD_SetState} $0 ${BST_CHECKED}
  ${NSD_OnClick} $0 NesqShortcutToggled

  ; Nothing stands between this page and files being written, so say so. NSIS
  ; only relabels the button by itself when the very next page is the install
  ; page, and here the reinstall page may or may not appear in between.
  SendMessage $mui.Button.Next ${WM_SETTEXT} 0 "STR:Install"
FunctionEnd

Function NesqShortcutToggled
  Pop $0 ; control handle, pushed by nsDialogs
  ${NSD_GetState} $0 $NesqDesktopShortcut
FunctionEnd

Function NesqChooseFolder
  Pop $0 ; control handle, pushed by nsDialogs
  nsDialogs::SelectFolderDialog "Choose where to install ${PRODUCTNAME}" "$INSTDIR"
  Pop $0
  ${If} $0 == "error"
    Return
  ${EndIf}
  ; Append the product name unless the chosen folder already ends in it - the
  ; same courtesy the stock Browse button does, and the difference between
  ; "C:\Apps\Nesq Bot" and forty loose files in "C:\Apps".
  StrLen $1 "${PRODUCTNAME}"
  IntOp $1 0 - $1
  StrCpy $2 "$0" "" $1
  ${If} $2 != "${PRODUCTNAME}"
    StrCpy $0 "$0\${PRODUCTNAME}"
  ${EndIf}
  StrCpy $INSTDIR "$0"
  ${NSD_SetText} $NesqPathLabel "$INSTDIR"
FunctionEnd

!macro NESQ_INSTFILES_SHOW
  SetCtlColors $mui.InstFilesPage "${NESQ_TEXT}" "${NESQ_BG}"
  SetCtlColors $mui.InstFilesPage.Text "${NESQ_MUTED}" "${NESQ_BG}"
  SendMessage $mui.InstFilesPage.Text ${WM_SETFONT} $NesqFontBody 0
  SendMessage $mui.InstFilesPage.ShowLogButton ${WM_SETFONT} $NesqFontBody 0
  !insertmacro NESQ_DARK_CONTROL $mui.InstFilesPage.ShowLogButton
  !insertmacro NESQ_DARK_CONTROL $mui.InstFilesPage.Log
  !insertmacro NESQ_PROGRESSBAR $mui.InstFilesPage.ProgressBar
!macroend

Function NesqInstFilesShow
  !insertmacro NESQ_INSTFILES_SHOW
FunctionEnd

Function un.NesqInstFilesShow
  !insertmacro NESQ_INSTFILES_SHOW
FunctionEnd

Function NesqFinishShow
  SetCtlColors $mui.FinishPage.Title "${NESQ_TEXT}" "${NESQ_BG}"
  SendMessage $mui.FinishPage.Title ${WM_SETFONT} $NesqFontTitle 0

  ; The body text is the install path, so treat it as one.
  SetCtlColors $mui.FinishPage.Text "${NESQ_MUTED}" "${NESQ_BG}"
  SendMessage $mui.FinishPage.Text ${WM_SETFONT} $NesqFontBody 0

  SendMessage $mui.FinishPage.Run ${WM_SETFONT} $NesqFontBody 0
  !insertmacro NESQ_DARK_CONTROL $mui.FinishPage.Run
FunctionEnd

;--------------------------------
; Uninstaller Pages
; 1. Confirm uninstall page
Var DeleteAppDataCheckbox
Var DeleteAppDataCheckboxState
!define /ifndef WS_EX_LAYOUTRTL         0x00400000
!define MUI_PAGE_CUSTOMFUNCTION_SHOW un.ConfirmShow
Function un.ConfirmShow ; Add add a `Delete app data` check box
  ; $1 inner dialog HWND
  ; $2 window DPI
  ; $3 style
  ; $4 x
  ; $5 y
  ; $6 width
  ; $7 height
  FindWindow $1 "#32770" "" $HWNDPARENT ; Find inner dialog

  ; Same dark treatment as the installer's interior pages. 1006 is the body
  ; text, 1000 the folder being removed.
  SetCtlColors $1 "${NESQ_TEXT}" "${NESQ_BG}"
  GetDlgItem $0 $1 1006
  SetCtlColors $0 "${NESQ_MUTED}" "${NESQ_BG}"
  SendMessage $0 ${WM_SETFONT} $NesqFontBody 0
  GetDlgItem $0 $1 1000
  SetCtlColors $0 "${NESQ_TEXT}" "${NESQ_SURFACE}"
  SendMessage $0 ${WM_SETFONT} $NesqFontBody 0
  !insertmacro NESQ_DARK_FIELD $0
  ; The folder box also carries a sunken 3D edge, which is a light rectangle
  ; drawn round a dark field in the non-client area - out of reach of both
  ; SetCtlColors and the dark theme. Clear the three border bits and ask the
  ; frame to recompute, which is what makes a style change take effect.
  IntOp $R9 0x00020200 ~ ; WS_EX_CLIENTEDGE | WS_EX_STATICEDGE
  System::Call 'user32::GetWindowLongW(p $0, i -20) i .r9'
  IntOp $9 $9 & $R9
  System::Call 'user32::SetWindowLongW(p $0, i -20, i $9)'
  IntOp $R9 0x00001000 ~ ; SS_SUNKEN
  System::Call 'user32::GetWindowLongW(p $0, i -16) i .r9'
  IntOp $9 $9 & $R9
  System::Call 'user32::SetWindowLongW(p $0, i -16, i $9)'
  ; SWP_NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
  System::Call 'user32::SetWindowPos(p $0, p 0, i 0, i 0, i 0, i 0, i 0x37)'
  GetDlgItem $0 $1 1029
  SetCtlColors $0 "${NESQ_MUTED}" "${NESQ_BG}"
  SendMessage $0 ${WM_SETFONT} $NesqFontSmall 0

  System::Call "user32::GetDpiForWindow(p r1) i .r2"
  ${If} $(^RTL) = 1
    StrCpy $3 "${__NSD_CheckBox_EXSTYLE} | ${WS_EX_LAYOUTRTL}"
    IntOp $4 50 * $2
  ${Else}
    StrCpy $3 "${__NSD_CheckBox_EXSTYLE}"
    IntOp $4 0 * $2
  ${EndIf}
  IntOp $5 100 * $2
  IntOp $6 400 * $2
  IntOp $7 25 * $2
  IntOp $4 $4 / 96
  IntOp $5 $5 / 96
  IntOp $6 $6 / 96
  IntOp $7 $7 / 96
  System::Call 'user32::CreateWindowEx(i r3, w "${__NSD_CheckBox_CLASS}", w "$(deleteAppData)", i ${__NSD_CheckBox_STYLE}, i r4, i r5, i r6, i r7, p r1, i0, i0, i0) i .s'
  Pop $DeleteAppDataCheckbox
  SendMessage $DeleteAppDataCheckbox ${WM_SETFONT} $NesqFontBody 1
  SetCtlColors $DeleteAppDataCheckbox "${NESQ_TEXT}" "${NESQ_BG}"
  !insertmacro NESQ_DARK_CONTROL $DeleteAppDataCheckbox
FunctionEnd
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE un.ConfirmLeave
Function un.ConfirmLeave
  SendMessage $DeleteAppDataCheckbox ${BM_GETCHECK} 0 0 $DeleteAppDataCheckboxState
FunctionEnd
!define MUI_PAGE_CUSTOMFUNCTION_PRE un.SkipIfPassive
!insertmacro MUI_UNPAGE_CONFIRM

; 2. Uninstalling Page
!define MUI_PAGE_CUSTOMFUNCTION_SHOW un.NesqInstFilesShow
!insertmacro MUI_UNPAGE_INSTFILES

;Languages
{{#each languages}}
!insertmacro MUI_LANGUAGE "{{this}}"
{{/each}}
!insertmacro MUI_RESERVEFILE_LANGDLL
{{#each language_files}}
  !include "{{this}}"
{{/each}}

; The bundler renders the language files without the product name in scope, so
; the three strings that interpolate it reach the user with the literal
; placeholder text still in them, reading "... is running!" with a pair of
; braces where the name should be. Restate them here, after the include, where
; the name does exist. Guarded on the language actually being bundled, so
; adding or removing languages cannot break the build.
!ifdef LANG_ENGLISH
  LangString appRunning ${LANG_ENGLISH} "${PRODUCTNAME} is running. Close it and try again."
  LangString appRunningOkKill ${LANG_ENGLISH} "${PRODUCTNAME} is running.$\nClick OK to close it and continue."
  LangString failedToKillApp ${LANG_ENGLISH} "Could not close ${PRODUCTNAME}. Close it yourself and try again."
!endif

Function .onInit
  ${GetOptions} $CMDLINE "/P" $PassiveMode
  ${IfNot} ${Errors}
    StrCpy $PassiveMode 1
  ${EndIf}

  ${GetOptions} $CMDLINE "/NS" $NoShortcutMode
  ${IfNot} ${Errors}
    StrCpy $NoShortcutMode 1
  ${EndIf}

  ${GetOptions} $CMDLINE "/UPDATE" $UpdateMode
  ${IfNot} ${Errors}
    StrCpy $UpdateMode 1
  ${EndIf}

  ; Default for the welcome page's checkbox, and the value silent and passive
  ; installs keep - they never see the page, and used to always get a shortcut.
  StrCpy $NesqDesktopShortcut 1

  !if "${DISPLAYLANGUAGESELECTOR}" == "true"
    !insertmacro MUI_LANGDLL_DISPLAY
  !endif

  !insertmacro SetContext

  ${If} $INSTDIR == "${PLACEHOLDER_INSTALL_DIR}"
    ; Set default install location
    !if "${INSTALLMODE}" == "perMachine"
      ${If} ${RunningX64}
        !if "${ARCH}" == "x64"
          StrCpy $INSTDIR "$PROGRAMFILES64\${PRODUCTNAME}"
        !else if "${ARCH}" == "arm64"
          StrCpy $INSTDIR "$PROGRAMFILES64\${PRODUCTNAME}"
        !else
          StrCpy $INSTDIR "$PROGRAMFILES\${PRODUCTNAME}"
        !endif
      ${Else}
        StrCpy $INSTDIR "$PROGRAMFILES\${PRODUCTNAME}"
      ${EndIf}
    !else if "${INSTALLMODE}" == "currentUser"
      StrCpy $INSTDIR "$LOCALAPPDATA\${PRODUCTNAME}"
    !endif

    Call RestorePreviousInstallLocation
  ${EndIf}


  !if "${INSTALLMODE}" == "both"
    !insertmacro MULTIUSER_INIT
  !endif
FunctionEnd


Section EarlyChecks
  ; Abort silent installer if downgrades is disabled
  !if "${ALLOWDOWNGRADES}" == "false"
  ${If} ${Silent}
    ; If downgrading
    ${If} $R0 = -1
      System::Call 'kernel32::AttachConsole(i -1)i.r0'
      ${If} $0 <> 0
        System::Call 'kernel32::GetStdHandle(i -11)i.r0'
        System::call 'kernel32::SetConsoleTextAttribute(i r0, i 0x0004)' ; set red color
        FileWrite $0 "$(silentDowngrades)"
      ${EndIf}
      Abort
    ${EndIf}
  ${EndIf}
  !endif

SectionEnd

Section WebView2
  ; Check if Webview2 is already installed and skip this section
  ${If} ${RunningX64}
    ReadRegStr $4 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${WEBVIEW2APPGUID}" "pv"
  ${Else}
    ReadRegStr $4 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW2APPGUID}" "pv"
  ${EndIf}
  ${If} $4 == ""
    ReadRegStr $4 HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW2APPGUID}" "pv"
  ${EndIf}

  ${If} $4 == ""
    ; Webview2 installation
    ;
    ; Skip if updating
    ${If} $UpdateMode <> 1
      !if "${INSTALLWEBVIEW2MODE}" == "downloadBootstrapper"
        Delete "$TEMP\MicrosoftEdgeWebview2Setup.exe"
        DetailPrint "$(webview2Downloading)"
        NSISdl::download "https://go.microsoft.com/fwlink/p/?LinkId=2124703" "$TEMP\MicrosoftEdgeWebview2Setup.exe"
        Pop $0
        ${If} $0 == "success"
          DetailPrint "$(webview2DownloadSuccess)"
        ${Else}
          DetailPrint "$(webview2DownloadError)"
          Abort "$(webview2AbortError)"
        ${EndIf}
        StrCpy $6 "$TEMP\MicrosoftEdgeWebview2Setup.exe"
        Goto install_webview2
      !endif

      !if "${INSTALLWEBVIEW2MODE}" == "embedBootstrapper"
        Delete "$TEMP\MicrosoftEdgeWebview2Setup.exe"
        File "/oname=$TEMP\MicrosoftEdgeWebview2Setup.exe" "${WEBVIEW2BOOTSTRAPPERPATH}"
        DetailPrint "$(installingWebview2)"
        StrCpy $6 "$TEMP\MicrosoftEdgeWebview2Setup.exe"
        Goto install_webview2
      !endif

      !if "${INSTALLWEBVIEW2MODE}" == "offlineInstaller"
        Delete "$TEMP\MicrosoftEdgeWebView2RuntimeInstaller.exe"
        File "/oname=$TEMP\MicrosoftEdgeWebView2RuntimeInstaller.exe" "${WEBVIEW2INSTALLERPATH}"
        DetailPrint "$(installingWebview2)"
        StrCpy $6 "$TEMP\MicrosoftEdgeWebView2RuntimeInstaller.exe"
        Goto install_webview2
      !endif

      Goto webview2_done

      install_webview2:
        DetailPrint "$(installingWebview2)"
        ; $6 holds the path to the webview2 installer
        ExecWait "$6 ${WEBVIEW2INSTALLERARGS} /install" $1
        ${If} $1 = 0
          DetailPrint "$(webview2InstallSuccess)"
        ${Else}
          DetailPrint "$(webview2InstallError)"
          Abort "$(webview2AbortError)"
        ${EndIf}
      webview2_done:
    ${EndIf}
  ${Else}
    !if "${MINIMUMWEBVIEW2VERSION}" != ""
      ${VersionCompare} "${MINIMUMWEBVIEW2VERSION}" "$4" $R0
      ${If} $R0 = 1
        update_webview:
          DetailPrint "$(installingWebview2)"
          ${If} ${RunningX64}
            ReadRegStr $R1 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate" "path"
          ${Else}
            ReadRegStr $R1 HKLM "SOFTWARE\Microsoft\EdgeUpdate" "path"
          ${EndIf}
          ${If} $R1 == ""
            ReadRegStr $R1 HKCU "SOFTWARE\Microsoft\EdgeUpdate" "path"
          ${EndIf}
          ${If} $R1 != ""
            ; Chromium updater docs: https://source.chromium.org/chromium/chromium/src/+/main:docs/updater/user_manual.md
            ; Modified from "HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Microsoft EdgeWebView\ModifyPath"
            ExecWait `"$R1" /install appguid=${WEBVIEW2APPGUID}&needsadmin=true` $1
            ${If} $1 = 0
              DetailPrint "$(webview2InstallSuccess)"
            ${Else}
              MessageBox MB_ICONEXCLAMATION|MB_ABORTRETRYIGNORE "$(webview2InstallError)" IDIGNORE ignore IDRETRY update_webview
              Quit
              ignore:
            ${EndIf}
          ${EndIf}
      ${EndIf}
    !endif
  ${EndIf}
SectionEnd

Section Install
  SetOutPath $INSTDIR

  !ifmacrodef NSIS_HOOK_PREINSTALL
    !insertmacro NSIS_HOOK_PREINSTALL
  !endif

  !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"

  ; Copy main executable
  File "${MAINBINARYSRCPATH}"

  ; Copy resources
  {{#each resources_dirs}}
    CreateDirectory "$INSTDIR\\{{this}}"
  {{/each}}
  {{#each resources}}
    File /a "/oname={{this.[1]}}" "{{no-escape @key}}"
  {{/each}}

  ; Copy external binaries
  {{#each binaries}}
    File /a "/oname={{this}}" "{{no-escape @key}}"
  {{/each}}

  ; Create file associations
  {{#each file_associations as |association| ~}}
    {{#each association.ext as |ext| ~}}
       !insertmacro APP_ASSOCIATE "{{ext}}" "{{or association.name ext}}" "{{association-description association.description ext}}" "$INSTDIR\${MAINBINARYNAME}.exe,0" "Open with ${PRODUCTNAME}" "$INSTDIR\${MAINBINARYNAME}.exe $\"%1$\""
    {{/each}}
  {{/each}}

  ; Register deep links
  {{#each deep_link_protocols as |protocol| ~}}
    WriteRegStr SHCTX "Software\Classes\\{{protocol}}" "URL Protocol" ""
    WriteRegStr SHCTX "Software\Classes\\{{protocol}}" "" "URL:${BUNDLEID} protocol"
    WriteRegStr SHCTX "Software\Classes\\{{protocol}}\DefaultIcon" "" "$\"$INSTDIR\${MAINBINARYNAME}.exe$\",0"
    WriteRegStr SHCTX "Software\Classes\\{{protocol}}\shell\open\command" "" "$\"$INSTDIR\${MAINBINARYNAME}.exe$\" $\"%1$\""
  {{/each}}

  ; Create uninstaller
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; Save $INSTDIR in registry for future installations
  WriteRegStr SHCTX "${MANUPRODUCTKEY}" "" $INSTDIR

  !if "${INSTALLMODE}" == "both"
    ; Save install mode to be selected by default for the next installation such as updating
    ; or when uninstalling
    WriteRegStr SHCTX "${UNINSTKEY}" $MultiUser.InstallMode 1
  !endif

  ; Remove old main binary if it doesn't match new main binary name
  ReadRegStr $OldMainBinaryName SHCTX "${UNINSTKEY}" "MainBinaryName"
  ${If} $OldMainBinaryName != ""
  ${AndIf} $OldMainBinaryName != "${MAINBINARYNAME}.exe"
    Delete "$INSTDIR\$OldMainBinaryName"
  ${EndIf}

  ; Save current MAINBINARYNAME for future updates
  WriteRegStr SHCTX "${UNINSTKEY}" "MainBinaryName" "${MAINBINARYNAME}.exe"

  ; Registry information for add/remove programs
  WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "${PRODUCTNAME}"
  WriteRegStr SHCTX "${UNINSTKEY}" "DisplayIcon" "$\"$INSTDIR\${MAINBINARYNAME}.exe$\""
  WriteRegStr SHCTX "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr SHCTX "${UNINSTKEY}" "Publisher" "${MANUFACTURER}"
  WriteRegStr SHCTX "${UNINSTKEY}" "InstallLocation" "$\"$INSTDIR$\""
  WriteRegStr SHCTX "${UNINSTKEY}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegDWORD SHCTX "${UNINSTKEY}" "NoModify" "1"
  WriteRegDWORD SHCTX "${UNINSTKEY}" "NoRepair" "1"

  ${GetSize} "$INSTDIR" "/M=uninstall.exe /S=0K /G=0" $0 $1 $2
  IntOp $0 $0 + ${ESTIMATEDSIZE}
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD SHCTX "${UNINSTKEY}" "EstimatedSize" "$0"

  !if "${HOMEPAGE}" != ""
    WriteRegStr SHCTX "${UNINSTKEY}" "URLInfoAbout" "${HOMEPAGE}"
    WriteRegStr SHCTX "${UNINSTKEY}" "URLUpdateInfo" "${HOMEPAGE}"
    WriteRegStr SHCTX "${UNINSTKEY}" "HelpLink" "${HOMEPAGE}"
  !endif

  ; Create start menu shortcut
  !insertmacro MUI_STARTMENU_WRITE_BEGIN Application
    Call CreateOrUpdateStartMenuShortcut
  !insertmacro MUI_STARTMENU_WRITE_END

  ; Desktop shortcut. The choice is made on the first page now rather than on
  ; the finish page, so it is acted on here. Passive and silent installs never
  ; see that page and keep the default of 1, which is the behaviour they had.
  ${If} $NesqDesktopShortcut = 1
  ${OrIf} $PassiveMode = 1
  ${OrIf} ${Silent}
    Call CreateOrUpdateDesktopShortcut
  ${EndIf}

  !ifmacrodef NSIS_HOOK_POSTINSTALL
    !insertmacro NSIS_HOOK_POSTINSTALL
  !endif

  ; Auto close this page for passive mode
  ${If} $PassiveMode = 1
    SetAutoClose true
  ${EndIf}
SectionEnd

Function .onInstSuccess
  ; Check for `/R` flag only in silent and passive installers because
  ; GUI installer has a toggle for the user to (re)start the app
  ${If} $PassiveMode = 1
  ${OrIf} ${Silent}
    ${GetOptions} $CMDLINE "/R" $R0
    ${IfNot} ${Errors}
      ${GetOptions} $CMDLINE "/ARGS" $R0
      nsis_tauri_utils::RunAsUser "$INSTDIR\${MAINBINARYNAME}.exe" "$R0"
    ${EndIf}
  ${EndIf}
FunctionEnd

Function un.onInit
  !insertmacro SetContext

  !if "${INSTALLMODE}" == "both"
    !insertmacro MULTIUSER_UNINIT
  !endif

  !insertmacro MUI_UNGETLANGUAGE

  ${GetOptions} $CMDLINE "/P" $PassiveMode
  ${IfNot} ${Errors}
    StrCpy $PassiveMode 1
  ${EndIf}

  ${GetOptions} $CMDLINE "/UPDATE" $UpdateMode
  ${IfNot} ${Errors}
    StrCpy $UpdateMode 1
  ${EndIf}
FunctionEnd

Section Uninstall

  !ifmacrodef NSIS_HOOK_PREUNINSTALL
    !insertmacro NSIS_HOOK_PREUNINSTALL
  !endif

  !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"

  ; Delete the app directory and its content from disk
  ; Copy main executable
  Delete "$INSTDIR\${MAINBINARYNAME}.exe"

  ; Delete resources
  {{#each resources}}
    Delete "$INSTDIR\\{{this.[1]}}"
  {{/each}}

  ; Delete external binaries
  {{#each binaries}}
    Delete "$INSTDIR\\{{this}}"
  {{/each}}

  ; Delete app associations
  {{#each file_associations as |association| ~}}
    {{#each association.ext as |ext| ~}}
      !insertmacro APP_UNASSOCIATE "{{ext}}" "{{or association.name ext}}"
    {{/each}}
  {{/each}}

  ; Delete deep links
  {{#each deep_link_protocols as |protocol| ~}}
    ReadRegStr $R7 SHCTX "Software\Classes\\{{protocol}}\shell\open\command" ""
    ${If} $R7 == "$\"$INSTDIR\${MAINBINARYNAME}.exe$\" $\"%1$\""
      DeleteRegKey SHCTX "Software\Classes\\{{protocol}}"
    ${EndIf}
  {{/each}}


  ; Delete uninstaller
  Delete "$INSTDIR\uninstall.exe"

  {{#each resources_ancestors}}
  RMDir /REBOOTOK "$INSTDIR\\{{this}}"
  {{/each}}
  RMDir "$INSTDIR"

  ; Remove shortcuts if not updating
  ${If} $UpdateMode <> 1
    !insertmacro DeleteAppUserModelId

    ; Remove start menu shortcut
    !insertmacro MUI_STARTMENU_GETFOLDER Application $AppStartMenuFolder
    !insertmacro IsShortcutTarget "$SMPROGRAMS\$AppStartMenuFolder\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    Pop $0
    ${If} $0 = 1
      !insertmacro UnpinShortcut "$SMPROGRAMS\$AppStartMenuFolder\${PRODUCTNAME}.lnk"
      Delete "$SMPROGRAMS\$AppStartMenuFolder\${PRODUCTNAME}.lnk"
      RMDir "$SMPROGRAMS\$AppStartMenuFolder"
    ${EndIf}
    !insertmacro IsShortcutTarget "$SMPROGRAMS\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    Pop $0
    ${If} $0 = 1
      !insertmacro UnpinShortcut "$SMPROGRAMS\${PRODUCTNAME}.lnk"
      Delete "$SMPROGRAMS\${PRODUCTNAME}.lnk"
    ${EndIf}

    ; Remove desktop shortcuts
    !insertmacro IsShortcutTarget "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    Pop $0
    ${If} $0 = 1
      !insertmacro UnpinShortcut "$DESKTOP\${PRODUCTNAME}.lnk"
      Delete "$DESKTOP\${PRODUCTNAME}.lnk"
    ${EndIf}
  ${EndIf}

  ; Remove registry information for add/remove programs
  !if "${INSTALLMODE}" == "both"
    DeleteRegKey SHCTX "${UNINSTKEY}"
  !else if "${INSTALLMODE}" == "perMachine"
    DeleteRegKey HKLM "${UNINSTKEY}"
  !else
    DeleteRegKey HKCU "${UNINSTKEY}"
  !endif

  ; Removes the Autostart entry for ${PRODUCTNAME} from the HKCU Run key if it exists.
  ; This ensures the program does not launch automatically after uninstallation if it exists.
  ; If it doesn't exist, it does nothing.
  ; We do this when not updating (to preserve the registry value on updates)
  ${If} $UpdateMode <> 1
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${PRODUCTNAME}"
  ${EndIf}

  ; Delete app data if the checkbox is selected
  ; and if not updating
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    ; Clear the install location $INSTDIR from registry
    DeleteRegKey SHCTX "${MANUPRODUCTKEY}"
    DeleteRegKey /ifempty SHCTX "${MANUKEY}"

    ; Clear the install language from registry
    DeleteRegValue HKCU "${MANUPRODUCTKEY}" "Installer Language"
    DeleteRegKey /ifempty HKCU "${MANUPRODUCTKEY}"
    DeleteRegKey /ifempty HKCU "${MANUKEY}"

    SetShellVarContext current
    RmDir /r "$APPDATA\${BUNDLEID}"
    RmDir /r "$LOCALAPPDATA\${BUNDLEID}"
  ${EndIf}

  !ifmacrodef NSIS_HOOK_POSTUNINSTALL
    !insertmacro NSIS_HOOK_POSTUNINSTALL
  !endif

  ; Auto close if passive mode or updating
  ${If} $PassiveMode = 1
  ${OrIf} $UpdateMode = 1
    SetAutoClose true
  ${EndIf}
SectionEnd

Function RestorePreviousInstallLocation
  ReadRegStr $4 SHCTX "${MANUPRODUCTKEY}" ""
  StrCmp $4 "" +2 0
    StrCpy $INSTDIR $4
FunctionEnd

Function Skip
  Abort
FunctionEnd

Function SkipIfPassive
  ${IfThen} $PassiveMode = 1  ${|} Abort ${|}
FunctionEnd
Function un.SkipIfPassive
  ${IfThen} $PassiveMode = 1  ${|} Abort ${|}
FunctionEnd

Function CreateOrUpdateStartMenuShortcut
  ; We used to use product name as MAINBINARYNAME
  ; migrate old shortcuts to target the new MAINBINARYNAME
  StrCpy $R0 0

  !insertmacro IsShortcutTarget "$SMPROGRAMS\$AppStartMenuFolder\${PRODUCTNAME}.lnk" "$INSTDIR\$OldMainBinaryName"
  Pop $0
  ${If} $0 = 1
    !insertmacro SetShortcutTarget "$SMPROGRAMS\$AppStartMenuFolder\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    StrCpy $R0 1
  ${EndIf}

  !insertmacro IsShortcutTarget "$SMPROGRAMS\${PRODUCTNAME}.lnk" "$INSTDIR\$OldMainBinaryName"
  Pop $0
  ${If} $0 = 1
    !insertmacro SetShortcutTarget "$SMPROGRAMS\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    StrCpy $R0 1
  ${EndIf}

  ${If} $R0 = 1
    Return
  ${EndIf}

  ; Skip creating shortcut if in update mode or no shortcut mode
  ; but always create if migrating from wix
  ${If} $WixMode = 0
    ${If} $UpdateMode = 1
    ${OrIf} $NoShortcutMode = 1
      Return
    ${EndIf}
  ${EndIf}

  !if "${STARTMENUFOLDER}" != ""
    CreateDirectory "$SMPROGRAMS\$AppStartMenuFolder"
    CreateShortcut "$SMPROGRAMS\$AppStartMenuFolder\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    !insertmacro SetLnkAppUserModelId "$SMPROGRAMS\$AppStartMenuFolder\${PRODUCTNAME}.lnk"
  !else
    CreateShortcut "$SMPROGRAMS\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    !insertmacro SetLnkAppUserModelId "$SMPROGRAMS\${PRODUCTNAME}.lnk"
  !endif
FunctionEnd

Function CreateOrUpdateDesktopShortcut
  ; We used to use product name as MAINBINARYNAME
  ; migrate old shortcuts to target the new MAINBINARYNAME
  !insertmacro IsShortcutTarget "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\$OldMainBinaryName"
  Pop $0
  ${If} $0 = 1
    !insertmacro SetShortcutTarget "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
    Return
  ${EndIf}

  ; Skip creating shortcut if in update mode or no shortcut mode
  ; but always create if migrating from wix
  ${If} $WixMode = 0
    ${If} $UpdateMode = 1
    ${OrIf} $NoShortcutMode = 1
      Return
    ${EndIf}
  ${EndIf}

  CreateShortcut "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
  !insertmacro SetLnkAppUserModelId "$DESKTOP\${PRODUCTNAME}.lnk"
FunctionEnd
