// PROTOTYPE — not compiled or run against a real JDownloader build from this
// environment (no JDownloader jar / plugin API available here to compile
// against). Modeled on JDownloader's own PluginForDecrypt shape (see e.g.
// src/jd/plugins/decrypter/BergFilesCom.java in the JDownloader source) but
// the exact base-class/annotation surface can drift between JD releases —
// expect to adjust imports/signatures to match the JDownloader version you
// actually install this into. See ../README.md for the full picture, install
// steps, and what to check first if it doesn't compile as-is.
//
// What this plugin does, and why it's shaped this way:
//
//   fuckingfast.co sits behind Cloudflare's bot check, which JDownloader's
//   plain HTTP engine cannot pass (no way to run the JS challenge, and its
//   TLS/HTTP fingerprint doesn't read as a real browser). This project's
//   Python side already solves that with Camoufox (a source-patched real
//   Firefox) driving the click-through to a pre-signed, cookie-free
//   dl.fuckingfast.co URL — see app/resolver.py.
//
//   Establishing that trust is a per-session cost, not a per-link one, so
//   rather than shelling out to Python fresh for every one of a repack's
//   ~150-250 part links, this plugin talks to resolver_daemon.py — a small
//   local HTTP service that keeps ONE browser alive and resolves links
//   against it as they come in. Start the daemon once; JDownloader just does
//   a localhost HTTP GET per link and hands the returned signed URL to its
//   own (perfectly good) download engine, which gets pause/resume, the
//   queue, and the GUI it already has for free.
//
// Requires: `python resolver_daemon.py` running locally (default
// http://127.0.0.1:8765) before you start grabbing fuckingfast.co links.

package jd.plugins.decrypter;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import jd.PluginWrapper;
import jd.controlling.ProgressController;
import jd.plugins.CryptedLink;
import jd.plugins.DecrypterException;
import jd.plugins.DecrypterPlugin;
import jd.plugins.DownloadLink;
import jd.plugins.PluginForDecrypt;

@DecrypterPlugin(
    revision = "$Revision$",
    interfaceVersion = 2,
    names = { "fuckingfast.co" },
    urls = { "https?://(www\\.)?fuckingfast\\.co/[A-Za-z0-9]+(#.*)?" },
    flags = { 0 }
)
public class FuckingFastCoStealth extends PluginForDecrypt {

    // Change this if you run resolver_daemon.py with --port on something else.
    private static final String DAEMON_BASE = "http://127.0.0.1:8765";
    private static final int CONNECT_TIMEOUT_MS = 5_000;
    // A resolve is a real page load + two button clicks in a real browser;
    // give it real headroom rather than JDownloader's usual short timeouts.
    private static final int READ_TIMEOUT_MS = 45_000;

    private static final Pattern SIGNED_URL_RE =
        Pattern.compile("\"signed_url\"\\s*:\\s*\"([^\"]+)\"");
    private static final Pattern FILENAME_RE =
        Pattern.compile("\"filename\"\\s*:\\s*\"([^\"]+)\"");
    private static final Pattern ERROR_RE =
        Pattern.compile("\"error\"\\s*:\\s*\"([^\"]*)\"");

    public FuckingFastCoStealth(PluginWrapper wrapper) {
        super(wrapper);
    }

    @Override
    public ArrayList<DownloadLink> decryptIt(CryptedLink param, ProgressController progress) throws Exception {
        ArrayList<DownloadLink> decryptedLinks = new ArrayList<DownloadLink>();
        final String originalUrl = param.toString();

        final String encoded = URLEncoder.encode(originalUrl, StandardCharsets.UTF_8.name());
        final URL resolveUrl = new URI(DAEMON_BASE + "/resolve?url=" + encoded).toURL();

        String body;
        int status;
        try {
            HttpURLConnection conn = (HttpURLConnection) resolveUrl.openConnection();
            conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
            conn.setReadTimeout(READ_TIMEOUT_MS);
            conn.setRequestMethod("GET");
            status = conn.getResponseCode();
            java.io.InputStream stream = (status == 200) ? conn.getInputStream() : conn.getErrorStream();
            StringBuilder sb = new StringBuilder();
            try (BufferedReader r = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
                String line;
                while ((line = r.readLine()) != null) sb.append(line);
            }
            body = sb.toString();
        } catch (java.net.ConnectException ce) {
            // Most likely cause: resolver_daemon.py isn't running. Say so
            // plainly rather than surfacing a bare "connection refused".
            logger.severe("Could not reach " + DAEMON_BASE + " — is resolver_daemon.py running? "
                + "(python jdownloader-plugin/resolver_daemon.py)");
            throw new DecrypterException("FuckingFast resolver daemon not reachable at "
                + DAEMON_BASE + ". Start it with: python resolver_daemon.py");
        }

        if (status != 200) {
            final Matcher em = ERROR_RE.matcher(body);
            final String msg = em.find() ? em.group(1) : ("HTTP " + status);
            logger.warning("Resolve failed for " + originalUrl + ": " + msg);
            // Cloudflare hiccup / captcha / dead link — surface it, don't crash the crawl.
            throw new DecrypterException("Resolve failed: " + msg);
        }

        final Matcher um = SIGNED_URL_RE.matcher(body);
        final Matcher fm = FILENAME_RE.matcher(body);
        if (!um.find() || !fm.find()) {
            logger.warning("Unexpected resolver response for " + originalUrl + ": " + body);
            return null; // JD convention: null means "decrypter is broken", not "0 links"
        }
        final String signedUrl = um.group(1);
        final String filename = fm.group(1);

        DownloadLink dl = createDownloadlink(signedUrl);
        dl.setFinalFileName(filename);
        // The signed URL is Range-capable and cookie-free (see app/resolver.py's
        // header comment) — plain direct HTTP is all JDownloader needs from here.
        decryptedLinks.add(dl);
        return decryptedLinks;
    }
}
