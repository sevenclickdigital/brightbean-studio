# Official YouTube icons

Downloaded on 2026-10-13 from the official [YouTube icon page](https://brand.youtube/youtube-icon/)
and its [asset archive](https://www.gstatic.com/marketing-cms/78/29/3e68a1414bb28d0b7e47b44c3c91/youtube-icon.zip).
These three PNGs are unchanged archive assets; they are not traced or recoloured.

| Local file | Archive asset | SHA-256 |
|---|---|---|
| icon-red.png | Red icon on transparent canvas | 1027b1b0517727adb9697155a270744381c3ce9b047b1c8bd8a9389dc7d07a83 |
| icon-black.png | Black icon on transparent canvas | b3d0f71c6d094af85651f918e69a66210dbcee23e5382511935b5ab9ca47b266 |
| icon-white.png | White icon on transparent canvas | fb0593aaafa8ee8e01e1f5782b3b45b584ec82eaaae5982781572cec0c828f3c |

The official red icon is rendered by `templates/partials/_youtube_icon.html`
on the connect-platform card, public client-onboarding row and account-selection
header. The unchanged PNG preserves its intrinsic clear space and proportions,
renders on white, and links to YouTube. At the user's request, the original compact
layout is restored: a 20px visible icon width in the platform card and onboarding
row, and 24px in the account-selection header. Image canvas heights are 26px and
31.2px respectively, preserving the supplied proportions and clear space.

`templates/partials/_youtube_attribution.html` renders the plain trade name
“YouTube” in the application's normal font, linking to the corresponding channel
or video. Mixed-platform content identifies its source within each YouTube block.
Text attribution does not reproduce the graphic wordmark or imply endorsement.

`templates/partials/_youtube_badge_icon.html` renders the red icon in the
sidebar's avatar badge, at a 16px visible width, where every other platform shows
a 16px SVG. It replaces the earlier treatment, which showed the trade name
“YouTube” above the channel name because no icon placement that small had been
accepted; the label was removed at the user's request, for consistency with the
other channels. The badge carries no link, because it sits inside the sidebar's
own anchor. Its row dims when a channel needs reconnecting: the opacity is
applied to the avatar alone, never to this asset.

The compact graphics fall below the previously selected 100px minimum visible
height; this implementation does not claim that minimum is satisfied. The
sidebar badge is the smallest of them and carries that caveat furthest. The PNG
canvas is 1255 × 1075; the visible mark occupies 827 × 579 pixels. A 26px-high
canvas gives a 20px-wide visible mark; 31.2px gives a 24px-wide visible mark;
the badge sets a 16px width and lets height follow, for a visible mark about
11px high.
Preserve proportions, clear space and
approved solid backgrounds; do not recolour the asset or apply opacity effects.
The numeric graphic minimum does not specify a minimum font size for plain text.
Reviewer acceptance of the overall remediation remains pending.

References: [API branding guidelines](https://developers.google.com/youtube/terms/branding-guidelines),
[developer policies](https://developers.google.com/youtube/terms/developer-policies#f.-user-experience),
[API Services Terms](https://developers.google.com/youtube/terms/api-services-terms-of-service),
[YouTube logo guidelines](https://brand.youtube/youtube-logo/).
