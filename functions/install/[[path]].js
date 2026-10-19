// Cloudflare Pages Function used by BatSign OTA installs.
//
// Public URL:
//   https://ikiraplus.pages.dev/install/<gbox-manifest-key>.plist
//
// The browser/iOS client stays on ikiraplus.pages.dev. We fetch the existing
// GBox manifest server-side and return its bytes directly (never redirecting
// the client to i.gbox.run), so the visible install origin remains our Pages
// domain while the manifest contents and signing flow stay unchanged.

export async function onRequest(context) {
  const request = context.request;

  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method Not Allowed", {
      status: 405,
      headers: { Allow: "GET, HEAD" },
    });
  }

  const url = new URL(request.url);
  const prefix = "/install/";

  if (!url.pathname.startsWith(prefix)) {
    return context.next();
  }

  // Keep the path exactly as the client supplied it, including any %3D Base64
  // padding escapes that GBox expects in its production manifest filename.
  const manifestPath = url.pathname.slice(prefix.length);

  if (!manifestPath || manifestPath.includes("/") || !manifestPath.endsWith(".plist")) {
    return new Response("Not Found", { status: 404 });
  }

  const upstreamURL = `https://i.gbox.run/${manifestPath}`;

  try {
    const upstream = await fetch(upstreamURL, {
      method: request.method,
      redirect: "follow",
      headers: {
        "User-Agent": request.headers.get("User-Agent") || "BatSign-Manifest-Proxy",
        Accept: request.headers.get("Accept") || "*/*",
      },
    });

    const headers = new Headers();
    headers.set(
      "Content-Type",
      upstream.headers.get("Content-Type") || "application/x-plist; charset=utf-8"
    );
    headers.set("Cache-Control", "no-store, no-cache, must-revalidate");
    headers.set("Pragma", "no-cache");
    headers.set("X-Content-Type-Options", "nosniff");

    const contentLength = upstream.headers.get("Content-Length");
    if (contentLength) headers.set("Content-Length", contentLength);

    return new Response(request.method === "HEAD" ? null : upstream.body, {
      status: upstream.status,
      headers,
    });
  } catch (error) {
    return new Response("Manifest proxy unavailable", {
      status: 502,
      headers: {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
      },
    });
  }
}
