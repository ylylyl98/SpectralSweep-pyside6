# Spectrum setup views and comparison

The Spectrum page keeps one pair of **1D Spectrum** / **2D Frame** tabs.
**Current setup** shows the active setup's live data and references. Switching
setups retains each setup's latest 1D and 2D acquisitions, selected tab, and view
ranges for the current application session. A setup with no acquisition starts
empty. Live acquisitions are not persisted in application preferences.

**Add spectrum** references and manually loaded spectra are remembered per
Sample ID across application restarts. Names, colors, visibility and list order
are restored from the sample profile; saved arrays are read from their original
CSV files. A failed save keeps a recovery copy in the profile so **Retry save**
remains available. Missing files are reported and retained for a later restore.
Changing Sample ID clears the previous sample's live display and restores only
the selected sample's reference list. New or duplicated sample settings do not
inherit another sample's spectra.

On first use with an older profile, the page recovers prior **Add spectrum**
exports under `<output root>/<sample>/Spectrum/<date>/` whose sidecars confirm
the exact Sample ID. Removing a reference affects only the list and remains
effective on restart; the CSV is retained. **Load spectra** starts in the current
sample's `Spectrum` folder (or its nearest existing parent for a new sample).

Choose **Compare** to show checked references from multiple setups alongside the
current live spectrum. Reference rows identify their acquisition setup. Hidden
references retain their checkbox state; Show all / Hide all operate on the
current list. Acquisition ownership comes from the frozen frame metadata, so
saving after switching devices cannot relabel an earlier acquisition.

**Hide wavelength gaps** is enabled by default in Compare. Disjoint sampled
wavelength bands appear side by side with real nm and reciprocal eV axes,
identical Y limits, and an explicit `//` marker stating the omitted interval.
Overlapping bands merge. Small gaps remain visible: a break must exceed both
10 nm and 10% of the narrower neighboring band. Internal gaps within one array
must also exceed ten typical sample steps. Zero intensity is still sampled data
and is never removed. Turn the option off for a continuous wavelength axis.

Segment data areas have proportional widths, keeping the same pixels per nm at
the fitted ranges. Manual zoom can change that scale. Segment geometry is reused
between live frames with unchanged wavelength coverage. Pixel spectra cannot
use wavelength-gap folding, and pixel/nm curves never share a displayed axis.

- **Fit live** fits the current live spectrum, excluding references from scaling.
- **Fit visible** fits all visible spectra, using segmented axes when enabled.
- **Auto Y** follows the chosen fit scope without changing X. Manual zoom pauses
  following on the adjusted axes; Fit live / Fit visible resume it.
- **Normalize** compares each curve relative to its own absolute peak. It refits
  Y and labels normalized intensity; original counts and exports are unchanged.

Loading a saved spectrum recovers its setup from a matching metadata sidecar.
CSV files without setup metadata are labeled **Imported / unassigned** and remain
available in either display mode. The controls affect 1D comparison; 2D detector
frames remain individual setup views.

Validation uses simulated controllers and offscreen Qt rendering; it does not
operate connected instruments.
