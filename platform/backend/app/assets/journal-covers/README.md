# Journal cover resources

Public journal cover thumbnails distributed with ScienceMate. These are fallback
images for papers without their own usable figure, not claims that a paper
appeared on the journal cover. The artwork belongs to the respective publishers.

`manifest.json` maps normalized journal identifiers to WebP images and public
source URLs. Image files are named by their SHA-256 digest; identical images are
stored once. Maximum size is 360 × 512 pixels with the original aspect ratio.

The application can serve these files without a network connection, browser,
or pre-existing runtime cache. Runtime downloads remain outside the repository.

To publish newly collected covers, run from `platform/backend`:

```bash
python -m app.services.feed.journal_cover_bundle --cache /path/to/feed-journal-covers
```

Review and commit this directory with the code. Browser profiles, credentials,
cookies, local paths and URL query parameters are excluded from the export.
