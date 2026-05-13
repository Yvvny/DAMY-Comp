# Picture Naming Rules

This document is for another program that needs to understand how Select Best names and recognizes picture files.

The important rule is:

```text
original filename stem + one space + generated tag + original extension
```

Example:

```text
25_1106D_11549.jpg
25_1106D_11549 PA B_Blue HAS.jpg
```

## 1. Supported Image Files

The app treats these extensions as image files:

```text
.jpg
.jpeg
.png
.webp
.bmp
.tif
.tiff
```

Extension matching is case-insensitive.

Examples that are accepted:

```text
photo.jpg
photo.JPG
photo.jpeg
photo.PNG
photo.webp
photo.bmp
photo.tif
photo.tiff
```

Examples that are not accepted:

```text
photo.heic
photo.gif
photo.pdf
photo.txt
```

The ranking CLI ignores image files whose filename contains:

```text
.crop.
```

Example ignored by ranking:

```text
25_1106D_11549.crop.jpg
```

## 2. Grouping Rule

Files are sorted by natural filename order first. Then the app scans them from top to bottom.

A file is a form marker when its filename contains parentheses:

```regex
\([^)]*\)
```

Any text inside the parentheses is accepted.

Examples of form marker files:

```text
25_1106D_(11549).jpg
001_(form).jpg
Student Order (A123).png
Form (John Smith).jpeg
abc().jpg
```

Examples of portrait files:

```text
25_1106D_11549.jpg
001_photo1.jpg
Student Order A123.png
Form John Smith.jpeg
```

Grouping behavior:

```text
001_before_first_form.jpg  -> ignored, no group yet
010_(form).jpg             -> group 1 form
011_photo.jpg              -> group 1 portrait
012_photo.jpg              -> group 1 portrait
020_(form).jpg             -> group 2 form
021_photo.jpg              -> group 2 portrait
030_(form).jpg             -> group 3 form
```

If a form marker has no following portraits before the next form marker, that group has a form and zero portraits.

## 3. Confirmed Rename Rule

When the user confirms a selected portrait, the app renames:

```text
the form marker file
each selected portrait file
```

The app does not rename unselected portraits.

The file extension is preserved exactly as it was.

Examples:

```text
photo.jpg  -> photo PA B_Blue HAS.jpg
photo.JPG  -> photo PA B_Blue HAS.JPG
photo.png  -> photo PA B_Blue HAS.png
photo.tiff -> photo PA B_Blue HAS.tiff
```

## 4. Form Tags vs Portrait Tags

Form files and portrait files receive different package prefixes.

Form tag:

```text
FP...
```

Portrait tag:

```text
P...
```

Using Package A, background `B_Blue`, pose `HAS`:

```text
Form:
25_1106D_(11549).jpg -> 25_1106D_(11549) FPA B_Blue HAS.jpg

Portrait:
25_1106D_11549.jpg -> 25_1106D_11549 PA B_Blue HAS.jpg
```

## 5. Required and Optional Parts

Package is required before confirm.

Background is required before confirm.

Add-ons are optional.

Pose is optional.

Therefore, the possible generated tag shapes are:

```text
<package> <background>
<package> <background> <pose>
<package> + <addon> <background>
<package> + <addon> <background> <pose>
<package> + <addon1> + <addon2> <background>
<package> + <addon1> + <addon2> <background> <pose>
<package> + <addon1> + <addon2> + <addon3> <background>
<package> + <addon1> + <addon2> + <addon3> <background> <pose>
<package> + <addon1> + <addon2> + <addon3> + <addon4> <background>
<package> + <addon1> + <addon2> + <addon3> + <addon4> <background> <pose>
```

The form version uses the form package token, such as `FPA`.

The portrait version uses the portrait package token, such as `PA`.

## 6. Package Tokens

Built-in package results:

| UI Label | Stored package value | Portrait token | Form token |
| --- | --- | --- | --- |
| No Package | PNP | PNP | FPNP |
| Package A | A | PA | FPA |
| Package B | B | PB | FPB |
| Package C | C | PC | FPC |
| Package D | D | PD | FPD |
| Package E | E | PE | FPE |
| Package Teacher | T | PT | FPT |
| Package Sibling | S | PS | FPS |

Using original files:

```text
Form original:     25_1106D_(11549).jpg
Portrait original: 25_1106D_11549.jpg
Background:        B_Blue
Pose:              HAS
```

All built-in package outputs:

| Package | Form result | Portrait result |
| --- | --- | --- |
| No Package | `25_1106D_(11549) FPNP B_Blue HAS.jpg` | `25_1106D_11549 PNP B_Blue HAS.jpg` |
| Package A | `25_1106D_(11549) FPA B_Blue HAS.jpg` | `25_1106D_11549 PA B_Blue HAS.jpg` |
| Package B | `25_1106D_(11549) FPB B_Blue HAS.jpg` | `25_1106D_11549 PB B_Blue HAS.jpg` |
| Package C | `25_1106D_(11549) FPC B_Blue HAS.jpg` | `25_1106D_11549 PC B_Blue HAS.jpg` |
| Package D | `25_1106D_(11549) FPD B_Blue HAS.jpg` | `25_1106D_11549 PD B_Blue HAS.jpg` |
| Package E | `25_1106D_(11549) FPE B_Blue HAS.jpg` | `25_1106D_11549 PE B_Blue HAS.jpg` |
| Package Teacher | `25_1106D_(11549) FPT B_Blue HAS.jpg` | `25_1106D_11549 PT B_Blue HAS.jpg` |
| Package Sibling | `25_1106D_(11549) FPS B_Blue HAS.jpg` | `25_1106D_11549 PS B_Blue HAS.jpg` |

## 7. Custom Package Results

For a custom package, the app uses the custom text as the portrait package token. It does not automatically add `P`.

For the form file, the app adds `F` before the custom package text.

Examples with background `B_Blue` and pose `HAS`:

| Custom package input | Form tag | Portrait tag |
| --- | --- | --- |
| `PX1` | `FPX1 B_Blue HAS` | `PX1 B_Blue HAS` |
| `A` | `FA B_Blue HAS` | `A B_Blue HAS` |
| `Custom Package` | `FCustom Package B_Blue HAS` | `Custom Package B_Blue HAS` |
| `VIP` | `FVIP B_Blue HAS` | `VIP B_Blue HAS` |

Full filename examples:

```text
25_1106D_(11549).jpg -> 25_1106D_(11549) FPX1 B_Blue HAS.jpg
25_1106D_11549.jpg   -> 25_1106D_11549 PX1 B_Blue HAS.jpg
```

## 8. Add-on Results

The app allows 0 to 4 add-ons.

Add-ons are placed after the package token and before the background token.

Add-ons are joined with:

```text
 + 
```

Quantity rule:

```text
quantity 1: <description>
quantity 2 or more: <quantity> (<description>)
```

Add-on input cleanup:

```text
leading/trailing spaces are removed
multiple spaces become one space
spaces around x or X are removed
```

Examples:

| User input | Qty | Add-on token |
| --- | ---: | --- |
| `Wallets` | 1 | `Wallets` |
| `5 x 7` | 1 | `5x7` |
| `5 X 7` | 1 | `5x7` |
| `  8  x  10  ` | 1 | `8x10` |
| `5x7` | 2 | `2 (5x7)` |
| `Wallets` | 3 | `3 (Wallets)` |

Possible add-on count outputs using portrait Package A, background `B_Blue`, pose `HAS`:

| Add-ons | Portrait tag |
| --- | --- |
| none | `PA B_Blue HAS` |
| `Wallets` | `PA + Wallets B_Blue HAS` |
| `Wallets`, `2 (5x7)` | `PA + Wallets + 2 (5x7) B_Blue HAS` |
| `Wallets`, `2 (5x7)`, `Retouch` | `PA + Wallets + 2 (5x7) + Retouch B_Blue HAS` |
| `Wallets`, `2 (5x7)`, `Retouch`, `Digital` | `PA + Wallets + 2 (5x7) + Retouch + Digital B_Blue HAS` |

Matching form tags:

| Add-ons | Form tag |
| --- | --- |
| none | `FPA B_Blue HAS` |
| `Wallets` | `FPA + Wallets B_Blue HAS` |
| `Wallets`, `2 (5x7)` | `FPA + Wallets + 2 (5x7) B_Blue HAS` |
| `Wallets`, `2 (5x7)`, `Retouch` | `FPA + Wallets + 2 (5x7) + Retouch B_Blue HAS` |
| `Wallets`, `2 (5x7)`, `Retouch`, `Digital` | `FPA + Wallets + 2 (5x7) + Retouch + Digital B_Blue HAS` |

## 9. Background Tokens

The selected background is placed after package and add-ons.

Built-in background outputs:

| UI key | Rename token | Portrait example with Package A and HAS |
| --- | --- | --- |
| B_Blue | B_Blue | `25_1106D_11549 PA B_Blue HAS.jpg` |
| B_CR | B_ChildRoom | `25_1106D_11549 PA B_ChildRoom HAS.jpg` |
| B_Lamb | B_Lamborghini | `25_1106D_11549 PA B_Lamborghini HAS.jpg` |
| B_Grey | B_Grey | `25_1106D_11549 PA B_Grey HAS.jpg` |
| B_NY | B_NewYork | `25_1106D_11549 PA B_NewYork HAS.jpg` |
| B_GrnGrdn | B_GreenGarden | `25_1106D_11549 PA B_GreenGarden HAS.jpg` |
| B_Arc | B_Arc | `25_1106D_11549 PA B_Arc HAS.jpg` |
| B_Space | B_Space | `25_1106D_11549 PA B_Space HAS.jpg` |
| B_OldLib | B_OldLibrary | `25_1106D_11549 PA B_OldLibrary HAS.jpg` |
| B_Lib | B_Library | `25_1106D_11549 PA B_Library HAS.jpg` |
| B_Amsterdam | B_Amsterdam | `25_1106D_11549 PA B_Amsterdam HAS.jpg` |
| B_Maserati | B_Maserati | `25_1106D_11549 PA B_Maserati HAS.jpg` |
| B_MigdalD | B_MigdalD | `25_1106D_11549 PA B_MigdalD HAS.jpg` |
| B_WaterF | B_WaterFall | `25_1106D_11549 PA B_WaterFall HAS.jpg` |
| B_AngelSt | B_AngelStand | `25_1106D_11549 PA B_AngelStand HAS.jpg` |
| B_Beach | B_Beach | `25_1106D_11549 PA B_Beach HAS.jpg` |

Form examples use `FPA` instead of `PA`:

```text
25_1106D_(11549) FPA B_Blue HAS.jpg
25_1106D_(11549) FPA B_ChildRoom HAS.jpg
25_1106D_(11549) FPA B_Lamborghini HAS.jpg
```

Custom background text is allowed and is sanitized before use.

Examples:

| Custom background input | Background token | Portrait result |
| --- | --- | --- |
| `B_Custom` | `B_Custom` | `25_1106D_11549 PA B_Custom HAS.jpg` |
| `Blue Room` | `Blue Room` | `25_1106D_11549 PA Blue Room HAS.jpg` |
| `B:Blue/Special` | `B-Blue-Special` | `25_1106D_11549 PA B-Blue-Special HAS.jpg` |

## 10. Pose Tokens

Built-in pose tokens:

```text
HAS
HOC
HOH
PRT
FB
```

Pose is optional.

Possible portrait outputs with Package A and background `B_Blue`:

| Pose | Portrait result |
| --- | --- |
| none | `25_1106D_11549 PA B_Blue.jpg` |
| HAS | `25_1106D_11549 PA B_Blue HAS.jpg` |
| HOC | `25_1106D_11549 PA B_Blue HOC.jpg` |
| HOH | `25_1106D_11549 PA B_Blue HOH.jpg` |
| PRT | `25_1106D_11549 PA B_Blue PRT.jpg` |
| FB | `25_1106D_11549 PA B_Blue FB.jpg` |

Possible form outputs with Package A and background `B_Blue`:

| Pose | Form result |
| --- | --- |
| none | `25_1106D_(11549) FPA B_Blue.jpg` |
| HAS | `25_1106D_(11549) FPA B_Blue HAS.jpg` |
| HOC | `25_1106D_(11549) FPA B_Blue HOC.jpg` |
| HOH | `25_1106D_(11549) FPA B_Blue HOH.jpg` |
| PRT | `25_1106D_(11549) FPA B_Blue PRT.jpg` |
| FB | `25_1106D_(11549) FPA B_Blue FB.jpg` |

Custom pose text is allowed and is sanitized before use.

Examples:

| Custom pose input | Pose token | Portrait result |
| --- | --- | --- |
| `SIT` | `SIT` | `25_1106D_11549 PA B_Blue SIT.jpg` |
| `Full Body 2` | `Full Body 2` | `25_1106D_11549 PA B_Blue Full Body 2.jpg` |
| `Pose:Left` | `Pose-Left` | `25_1106D_11549 PA B_Blue Pose-Left.jpg` |

## 11. Token Sanitizing

Package, add-on, background, and pose tokens are sanitized before renaming.

Rules:

```text
1. Remove leading and trailing spaces.
2. Replace repeated whitespace with one space.
3. Replace invalid filename characters with -.
```

Invalid filename characters:

```text
< > : " / \ | ? *
```

Sanitizing examples:

| Raw input | Sanitized token |
| --- | --- |
| `  Blue  Room  ` | `Blue Room` |
| `B:Blue` | `B-Blue` |
| `B/Blue` | `B-Blue` |
| `B\Blue` | `B-Blue` |
| `B|Blue` | `B-Blue` |
| `B?Blue` | `B-Blue` |
| `B*Blue` | `B-Blue` |
| `<Blue>` | `-Blue-` |
| `"Blue"` | `-Blue-` |
| `B:Blue / Special` | `B-Blue - Special` |

## 12. Full Combination Examples

Base files:

```text
Form:     25_1106D_(11549).jpg
Portrait: 25_1106D_11549.jpg
```

Minimum valid confirm, no pose, no add-ons:

```text
Package A + B_Blue

Form:
25_1106D_(11549) FPA B_Blue.jpg

Portrait:
25_1106D_11549 PA B_Blue.jpg
```

Package + background + pose:

```text
Package A + B_Blue + HAS

Form:
25_1106D_(11549) FPA B_Blue HAS.jpg

Portrait:
25_1106D_11549 PA B_Blue HAS.jpg
```

Package + one add-on + background + pose:

```text
Package A + Wallets + B_Blue + HAS

Form:
25_1106D_(11549) FPA + Wallets B_Blue HAS.jpg

Portrait:
25_1106D_11549 PA + Wallets B_Blue HAS.jpg
```

Package + two add-ons + background + pose:

```text
Package A + Wallets + 2 (5x7) + B_Blue + HAS

Form:
25_1106D_(11549) FPA + Wallets + 2 (5x7) B_Blue HAS.jpg

Portrait:
25_1106D_11549 PA + Wallets + 2 (5x7) B_Blue HAS.jpg
```

Package + four add-ons + background + pose:

```text
Package A + Wallets + 2 (5x7) + Retouch + Digital + B_Blue + HAS

Form:
25_1106D_(11549) FPA + Wallets + 2 (5x7) + Retouch + Digital B_Blue HAS.jpg

Portrait:
25_1106D_11549 PA + Wallets + 2 (5x7) + Retouch + Digital B_Blue HAS.jpg
```

No Package option:

```text
No Package + B_Blue + HAS

Form:
25_1106D_(11549) FPNP B_Blue HAS.jpg

Portrait:
25_1106D_11549 PNP B_Blue HAS.jpg
```

Teacher package:

```text
Package Teacher + B_Blue + HAS

Form:
25_1106D_(11549) FPT B_Blue HAS.jpg

Portrait:
25_1106D_11549 PT B_Blue HAS.jpg
```

Sibling package:

```text
Package Sibling + B_Blue + HAS

Form:
25_1106D_(11549) FPS B_Blue HAS.jpg

Portrait:
25_1106D_11549 PS B_Blue HAS.jpg
```

Custom package:

```text
Custom package PX1 + B_Blue + HAS

Form:
25_1106D_(11549) FPX1 B_Blue HAS.jpg

Portrait:
25_1106D_11549 PX1 B_Blue HAS.jpg
```

Custom background and custom pose:

```text
Package A + Blue Room + Full Body 2

Form:
25_1106D_(11549) FPA Blue Room Full Body 2.jpg

Portrait:
25_1106D_11549 PA Blue Room Full Body 2.jpg
```

Sanitized custom values:

```text
Package input:    VIP/Gold
Background input: B:Blue/Special
Pose input:       Pose:Left

Form:
25_1106D_(11549) FVIP-Gold B-Blue-Special Pose-Left.jpg

Portrait:
25_1106D_11549 VIP-Gold B-Blue-Special Pose-Left.jpg
```

Multiple selected portraits get the same generated tag:

```text
Form:
25_1106D_(11549).jpg
-> 25_1106D_(11549) FPA B_Blue HAS.jpg

Portrait 1:
25_1106D_11549_1.jpg
-> 25_1106D_11549_1 PA B_Blue HAS.jpg

Portrait 2:
25_1106D_11549_2.jpg
-> 25_1106D_11549_2 PA B_Blue HAS.jpg
```

Unselected portraits remain unchanged:

```text
25_1106D_11549_3.jpg
```

## 13. Name Conflict Results

If the target filename already exists, the app appends a counter before the extension.

Counter format:

```text
(1)
(2)
(3)
```

The counter starts at `1`.

Example target:

```text
25_1106D_11549 PA B_Blue HAS.jpg
```

If that already exists, the result becomes:

```text
25_1106D_11549 PA B_Blue HAS(1).jpg
```

If both already exist:

```text
25_1106D_11549 PA B_Blue HAS.jpg
25_1106D_11549 PA B_Blue HAS(1).jpg
```

Then the result becomes:

```text
25_1106D_11549 PA B_Blue HAS(2).jpg
```

Form conflict example:

```text
25_1106D_(11549) FPA B_Blue HAS.jpg
25_1106D_(11549) FPA B_Blue HAS(1).jpg
```

Portrait conflict example:

```text
25_1106D_11549 PA B_Blue HAS.jpg
25_1106D_11549 PA B_Blue HAS(1).jpg
```

## 14. Move-to-Folder Conflict Results

When moving files to folders such as `unpaid` or `Need Help`, the filename is kept unless a file with the same name already exists in the target folder.

If there is a conflict, the app appends a counter before the extension:

```text
photo.jpg
photo(1).jpg
photo(2).jpg
```

This move-to-folder conflict style uses `(1)`, not `-1`.

The older ranking CLI `safe_move` function uses `-1`, `-2` when moving files into `best` or `done`:

```text
photo.jpg
photo-1.jpg
photo-2.jpg
```

## 15. Processed File Detection

When the app loads a folder, it skips already processed groups by checking the form filename stem.

The skip check looks for `FP` followed by an uppercase letter anywhere in the stem:

Processed form pattern:

```regex
FP[A-Z]
```

Examples considered processed:

```text
25_1106D_(11549) FPA B_Blue HAS.jpg
25_1106D_(11549) FPNP B_Blue.jpg
25_1106D_(11549) FPX1 B_Blue HAS.jpg
```

Examples not considered processed:

```text
25_1106D_(11549).jpg
25_1106D_(11549) PA B_Blue HAS.jpg
25_1106D_(11549)_FPA_B_Blue_HAS.jpg
25_1106D_(11549) FVIP B_Blue HAS.jpg
25_1106D_(11549) FP1 B_Blue HAS.jpg
```

Important custom package caveat:

```text
Custom package PX1 -> form tag FPX1 -> processed, because FP is followed by X.
Custom package VIP -> form tag FVIP -> not processed by the app's skip check.
Custom package P1  -> form tag FP1  -> not processed by the app's skip check, because 1 is not A-Z.
```

The undo button's suffix check is different. It looks for a space followed by `FP` for forms:

```regex
.*\sFP.+
```

Portrait suffix check for the undo button:

```regex
.*\sP.+
```

Examples matching the portrait suffix check:

```text
25_1106D_11549 PA B_Blue HAS.jpg
25_1106D_11549 PNP B_Blue.jpg
25_1106D_11549 PX1 B_Blue HAS.jpg
```

Examples not matching the portrait suffix check:

```text
25_1106D_11549 VIP B_Blue HAS.jpg
25_1106D_11549 A B_Blue HAS.jpg
```

## 16. Undo Fallback Detection

Undo fallback removes an appended suffix from the end of the filename stem.

Form undo pattern:

```regex
^(.*)\s(FP.+)$
```

Portrait undo pattern:

```regex
^(.*)\s(P.+)$
```

Examples:

```text
25_1106D_(11549) FPA B_Blue HAS.jpg
-> 25_1106D_(11549).jpg

25_1106D_11549 PA B_Blue HAS.jpg
-> 25_1106D_11549.jpg

25_1106D_11549 PA + Wallets + 2 (5x7) B_Blue HAS.jpg
-> 25_1106D_11549.jpg
```

Important: undo fallback removes the final ` FP...` or ` P...` part.

Custom package caveat:

```text
25_1106D_11549 PX1 B_Blue HAS.jpg
-> 25_1106D_11549.jpg

25_1106D_11549 VIP B_Blue HAS.jpg
-> fallback undo does not recognize this as a portrait suffix, because it does not start with P.
```

## 17. Parser Guidance for Another Program

To identify a renamed form file that follows the normal built-in-package style or a custom package starting with `P`:

```regex
^(?P<base>.+)\s(?P<tag>FP.+)$
```

To identify a renamed portrait file that uses a built-in package or a custom package starting with `P`:

```regex
^(?P<base>.+)\s(?P<tag>P.+)$
```

For custom packages that do not start with `P`, such as `VIP`, the generated portrait tag does not have a reliable universal prefix:

```text
25_1106D_11549 VIP B_Blue HAS.jpg
```

For those files, another program should use known job context, known selected package values, or the background/pose/add-on list to parse the tag. The form tag will start with `F` plus the custom package:

```text
25_1106D_(11549) FVIP B_Blue HAS.jpg
```

Use the filename stem only, not the extension.

Recommended parser behavior:

```text
1. Split extension from filename.
2. Work with the stem.
3. If stem matches final " FP...", treat it as a renamed form.
4. If stem matches final " P...", treat it as a renamed portrait.
5. The base name is everything before that final tag.
6. The tag is everything after the separating space.
```

Example:

```text
Filename:
25_1106D_11549 PA + Wallets B_Blue HAS.jpg

Stem:
25_1106D_11549 PA + Wallets B_Blue HAS

Base:
25_1106D_11549

Tag:
PA + Wallets B_Blue HAS

Extension:
.jpg
```

## 18. Current Proofing Import Paid Orders Naming

This section describes the current DAMYComp Proofing behavior when the user clicks:

```text
Import Paid Orders
```

The importer copies PhotoDeck paid-order assets into:

```text
<proofing job folder>/Orders
```

It copies two image types:

```text
original
proof
```

Proofing paid-order import currently recognizes these image extensions while copying:

```text
.jpg
.jpeg
.png
.webp
.bmp
.gif
.tif
.tiff
```

### Original Image Disk Naming

Original images are renamed on disk when copied into `Orders`.

Current rule:

```text
original filename stem + one space + generated portrait tag + original extension
```

This uses the portrait tag style from this document. It does not use form tags.

Therefore package tokens are:

```text
PNP, PA, PB, PC, PD, PE, P7, PT, PS
```

Not:

```text
FPNP, FPA, FPB, FPC, FPD, FPE, FP7, FPT, FPS
```

Current Proofing paid-order import does not add a pose token because PhotoDeck paid-order receipts currently do not provide pose information.

So the current generated tag shape is:

```text
<portrait package token> <background>
<portrait package token> + <addon1> <background>
<portrait package token> <digital editing> <background>
<portrait package token> + <addon1> <digital editing> <background>
<portrait package token> + <addon1> + <addon2> <background>
<portrait package token> + <addon1> + <addon2> + <addon3> <background>
<portrait package token> + <addon1> + <addon2> + <addon3> + <addon4> <background>
```

Examples:

```text
26_0323A_25241.JPG
-> 26_0323A_25241 PNP + 1-8x10 B_Grey.JPG

25_1106D_11549.jpg
-> 25_1106D_11549 PA + 2-3x5 Photo Magnets B_Blue.jpg

x.png
-> x PT B_OldLibrary.png
```

### Proof Image Disk Naming

Proof images are not renamed with package/add-on/background tags when copied into `Orders`.

Current rule:

```text
proof source filename is copied as-is unless the same original/proof photo appears multiple times in the same paid order
```

Examples:

```text
26_0323A_25241_OldLibrary.JPG
-> 26_0323A_25241_OldLibrary.JPG

26_0323A_25524_B_Villa.jpg
-> 26_0323A_25524_B_Villa.jpg
```

If a file with the same destination name already exists in `Orders`, the importer appends a Windows-style counter:

```text
filename.jpg
filename (2).jpg
filename (3).jpg
```

This counter behavior applies to both original and proof copies.

### Duplicate Same-Photo Order Naming

If the same original/proof photo appears more than once in the same PhotoDeck paid order, the importer does not rely on Windows-style `(2)` filenames for that duplicate set.

Instead, it inserts a duplicate index into both the original filename and the proof filename.

The index is inserted after the first two underscore-separated tokens:

```text
<prefix1>_<prefix2>_<index>_<rest>
```

Example source ids:

```text
original id: 26_0323A_25702
proof id:    26_0323A_25702_B_Blossoms
```

If that same photo has two ordered lines, the copied files become:

```text
26_0323A_1_25702 PNP + 2-3x5 Photo Magnets B_Grey
26_0323A_1_25702_B_Blossoms

26_0323A_2_25702 PNP + 4-4x5 B_Grey
26_0323A_2_25702_B_Blossoms
```

The original and proof copies use the same duplicate index for the same ordered line.

If a photo appears only once in the order, no duplicate index is inserted.

### Current Package Conversion

The current Proofing importer converts common PhotoDeck package values to portrait package tokens:

| PhotoDeck/package value | Current original tag token |
| --- | --- |
| No Package | PNP |
| PNP | PNP |
| A / Package A / PA | PA |
| B / Package B / PB | PB |
| C / Package C / PC | PC |
| D / Package D / PD | PD |
| E / Package E / PE | PE |
| 7 / Package 7 / P7 | P7 |
| T / Teacher / Package Teacher / PT | PT |
| S / Sibling / Package Sibling / PS | PS |

If the package value is not recognized, the current importer uses the cleaned package text as-is.

Example:

```text
VIP
-> VIP
```

### Current Add-on Conversion

The current Proofing importer:

```text
1. Handles Digital Editing separately; it is not treated as an add-on.
2. Skips empty/no add-on/as-is values.
3. Recognizes the known PhotoDeck add-on text values below.
4. Removes spaces around x or X.
5. Removes spaces around hyphens.
6. Does not convert the leading number in an add-on into quantity form.
```

Important: in PhotoDeck paid-order receipt text, a leading number inside the add-on description is part of the product description, not the order quantity. The actual order quantity comes from the separate Qty column.

Known PhotoDeck add-on text values and current filename output:

| PhotoDeck text | Filename token |
| --- | --- |
| `1-10x13` | `1-10x13` |
| `1-8x10 Photo Calendar` | `1-8x10 Photo Calendar` |
| `1-8x10` | `1-8x10` |
| `2-5x7` | `2-5x7` |
| `4-4x5` | `4-4x5` |
| `8 - 2.5x3.5 Jumbo Wallets` | `8-2.5x3.5 Jumbo Wallets` |
| `2 - 3x5 Photo Magnets` | `2-3x5 Photo Magnets` |
| `1 Digital Image` | `1 Digital Image` |
| `2 - Acrylic Key Holders with Pictures` | `2-Acrylic Key Holders with Pictures` |
| `1-11x14` | `1-11x14` |

Examples:

```text
1-8x10
-> 1-8x10

2-5 x 7
-> 2-5x7

2 - 3x5 Photo Magnets
-> 2-3x5 Photo Magnets

8 - 2.5 x 3.5 Jumbo Wallets
-> 8-2.5x3.5 Jumbo Wallets

1 Digital Image
-> 1 Digital Image
```

Add-ons are inserted after the package token with:

```text
 + 
```

Example:

```text
PA + 2-3x5 Photo Magnets B_Blue
```

### Current Digital Editing Conversion

Digital Editing is placed after add-ons and before background.

Current values:

| PhotoDeck text | Filename token |
| --- | --- |
| `As Is` | omitted |
| `Pimples Removal` | `DE_Pimples Removal` |
| `Whiten Teeth` | `DE_Whiten Teeth` |

Examples:

```text
PNP DE_Whiten Teeth B_Grey

PNP + 2-3x5 Photo Magnets DE_Whiten Teeth B_Grey
```

### Current Background Conversion

The current Proofing importer first reads the PhotoDeck receipt `Backgrounds:` value.

If the receipt background is empty or means no real background choice, such as:

```text
As is
as is
No add-ons
None
```

then the importer falls back to the proof image id/name and looks for the final background segment:

```text
_B_<background>
```

Examples:

```text
26_0323A_25313_B_USAOfficial
-> B_USAOfficial

26_0323A_25524_B_Villa
-> B_Villa

26_0323A_25369_B_SchoolYard
-> B_SchoolYard
```

After choosing the receipt background or the proof-id fallback background, the importer maps known background keys to the rename tokens from this document.

Known PhotoDeck background text values and current filename output:

| PhotoDeck text | Filename token |
| --- | --- |
| `B_New York` | `B_NewYork` |
| `B_Dubai` | `B_Dubai` |
| `B_London` | `B_London` |
| `B_Central Park` | `B_CentralPark` |
| `B_Green Garden` | `B_GreenGarden` |
| `B_Arc` | `B_Arc` |
| `B_Yard` | `B_Yard` |
| `B_Child Room` | `B_ChildRoom` |
| `B_Child Room Grads` | `B_ChildRoomGrads` |
| `B_Quiet Library` | `B_QuietLibrary` |
| `B_Library` | `B_Library` |
| `B_Thanksgiving` | `B_Thanksgiving` |
| `B_The_Pathway` | `B_ThePathway` |
| `B_Fall 15` | `B_Fall15` |
| `B_Fall` | `B_Fall` |
| `B_Christmas 517` | `B_Christmas517` |
| `B_Winter 5` | `B_Winter5` |
| `B_Lighted Tree` | `B_LightedTree` |
| `B_Wonder World` | `B_WonderWorld` |
| `BH_Lamborghini Blue` | `BH_LamborghiniBlue` |
| `BH_Lamborghini` | `BH_Lamborghini` |
| `BH_City_Night` | `BH_CityNight` |
| `BH_Lamborghini Orange` | `BH_LamborghiniOrange` |
| `B_Grey` | `B_Grey` |
| `B_Blue` | `B_Blue` |
| `B_White` | `B_White` |
| `B_Burgundy` | `B_Burgundy` |
| `B_Ferrari Blue` | `B_FerrariBlue` |
| `B_Brown` | `B_Brown` |
| `B_Ferrari` | `B_Ferrari` |
| `B_Ferrari_Gold` | `B_FerrariGold` |
| `B_Angel Stand` | `B_AngelStand` |
| `B_Angel Stand Winter` | `B_AngelStandWinter` |
| `B_White House 2` | `B_WhiteHouse2` |
| `B_White House` | `B_WhiteHouse` |
| `B_NYC Night` | `B_NYCNight` |

Additional older aliases are still accepted:

```text
B_OldLib
-> B_OldLibrary

B_Lib
-> B_Library

B_CR
-> B_ChildRoom

B_NY
-> B_NewYork
```

If the background value is not recognized, the importer uses the cleaned background text as-is.

Invalid filename characters in package, add-on, and background tokens are replaced with:

```text
-
```
