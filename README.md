# freeapps

The Arabic and English app catalogs (`IPA-AR.json` and `IPA-EN.json`) are updated on `main` by the GitHub Actions sync workflow.

Production: https://ikiraplus.pages.dev/

## Deployment verification

After a catalog sync, verify the **Cloudflare Pages** check for the latest commit has completed successfully. A successful GitHub Actions sync does not mean Cloudflare has published the same commit yet.

If https://ikiraplus.pages.dev/IPA-AR.json does not match `IPA-AR.json` on `main`, inspect or retry the deployment from the Cloudflare Pages dashboard. Avoid editing or truncating the catalog to address a deployment delay.
