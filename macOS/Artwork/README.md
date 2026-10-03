# Meter artwork

`Bars.png` is the app-icon source, a 1254-pixel PNG produced with an image-generation model and then stripped of embedded metadata, so the file carries only pixels. `docs/images/icon-256.png` is the downscaled copy the README embeds. Run `scripts/build-icons.sh` after changing it to regenerate `macOS/App/Resources/Bars.icns`. Commit both files. The Command Line Tools build copies the icon into the app bundle; Xcode includes it through the synchronized App group.

The menu-bar companion is drawn by `macOS/App/BarsIcon.swift`. Keep the short, medium and long bar order consistent across both marks. Its whole-point geometry is tuned for 18-point display at 1x and 2x. It is a template image, so macOS supplies its colour.
