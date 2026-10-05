# Member D: headphone catalog v4 data package

Download `headphone-catalog-v4-data.zip` from this directory and extract it locally.

- 40 fictional headphone products, with English brand/model/color names and HKD prices.
- 400 synthetic English reviews: 10 per product, each with its own rating out of 5.0.
- Product ratings are the average of all reviews, rounded to one decimal place.
- 10 products contain simulated manipulated reviews. Labels live separately under `evaluation/` and must not be given to a detection agent.
- JSON seeds, review summaries, observations, documentation, and an offline SQLite snapshot are included.

This commit supplies the data package. It does not integrate reviews into the current repository application or change its active product seed, public contracts, wallet, stock, or order records. The SQLite file inside the archive is a demonstration snapshot, not a replacement for an existing team database.

The archive's `catalog-v4.md` references `scripts/update_product_names.py` from the full v4 project; that script is not included in this data-only package or guaranteed to exist in this repository. Do not run a database reset to rename products.

All 40 purchasable products and all reviews are fictional demo data. JD observations are separate incomplete evidence records. Color/tag filtering has not been added by this upload.

Archive SHA-256: `4f85f64eedf1a2e7eb4dc76d8c156b27ba70efcb7d8fcd2b9866f0d15a01bc7c`
