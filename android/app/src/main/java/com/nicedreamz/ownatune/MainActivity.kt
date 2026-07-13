package com.nicedreamz.ownatune

import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.util.Log
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.URLUtil
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.browser.customtabs.CustomTabsIntent
import androidx.core.view.WindowCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID
import java.util.concurrent.Executors

/**
 * Song Forge Android shell. A thin WebView pointed at Matt's remote server.
 *
 * The installed app IS the account: we resolve the live app URL from a pointer
 * file (survives server moves), fall back to the permanent domain, introduce
 * ourselves with a stable per-install device id, and expose a native purchase
 * bridge the web page calls. Mirrors the iOS App.swift shell.
 */
class MainActivity : ComponentActivity() {

    companion object {
        private const val TAG = "SongForge"
        const val PERMANENT_URL = "https://songforge.nicedreamzwholesale.com"
        const val POINTER_URL =
            "https://nicedreamzwholesale.com/songs/songforge_app_url.txt"
        private const val PREFS = "songforge"
        private const val KEY_DEVICE = "songforge.device"

        // JS shim: the web page calls
        //   window.webkit.messageHandlers.buy.postMessage('songs30')
        // (the iOS/WKWebView convention). We redefine that path to forward into
        // the native @JavascriptInterface bridge. Injected at document start so
        // it exists before any page script runs.
        private const val BRIDGE_SHIM = """
            (function () {
              window.webkit = window.webkit || {};
              window.webkit.messageHandlers = window.webkit.messageHandlers || {};
              window.webkit.messageHandlers.buy = {
                postMessage: function (pack) {
                  try { AndroidBuy.buy(String(pack)); } catch (e) {}
                }
              };
            })();
        """
    }

    private lateinit var web: WebView

    /** Host of the resolved base URL; anything else opens in an external browser. */
    private var baseHost: String = Uri.parse(PERMANENT_URL).host ?: ""

    private val io = Executors.newSingleThreadExecutor()

    /** Stable per-install id: a UUID created once and persisted (iOS uses IDFV). */
    private fun deviceId(): String {
        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        prefs.getString(KEY_DEVICE, null)?.let { return it }
        val id = UUID.randomUUID().toString()
        prefs.edit().putString(KEY_DEVICE, id).apply()
        return id
    }

    /**
     * Resolve the live base: GET the pointer file, trim it, accept it only if
     * it's a short https url; otherwise fall back to PERMANENT_URL.
     */
    private fun liveBase(): String {
        try {
            val conn = (URL(POINTER_URL).openConnection() as HttpURLConnection).apply {
                connectTimeout = 4000
                readTimeout = 4000
                requestMethod = "GET"
            }
            if (conn.responseCode == 200) {
                val text = conn.inputStream.bufferedReader().use { it.readText() }.trim()
                if (text.startsWith("https://") && text.length < 200) {
                    return text
                }
            }
            conn.disconnect()
        } catch (e: Exception) {
            Log.w(TAG, "pointer fetch failed, using permanent url", e)
        }
        return PERMANENT_URL
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, true)

        web = WebView(this).apply {
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
            setBackgroundColor(0xFF0C0A08.toInt())
        }
        setContentView(web)

        configureWebView(web)

        // Resolve the base off the main thread (pointer file may be slow), then
        // load base/?device=<id>&native=1 on the UI thread.
        io.execute {
            val base = liveBase()
            val host = Uri.parse(base).host ?: baseHost
            val id = deviceId()
            val target = "$base/?device=$id&native=1"
            runOnUiThread {
                baseHost = host
                web.loadUrl(target)
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView(web: WebView) {
        val s = web.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true
        @Suppress("DEPRECATION")
        s.databaseEnabled = true
        s.mediaPlaybackRequiresUserGesture = false   // autoplay generated songs
        s.javaScriptCanOpenWindowsAutomatically = true
        s.setSupportMultipleWindows(true)
        s.loadWithOverviewMode = true
        s.useWideViewPort = true
        s.cacheMode = WebSettings.LOAD_DEFAULT

        // Cookies — the app relies on an 'oat' session cookie.
        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setAcceptThirdPartyCookies(web, true)
        }

        // Native purchase bridge.
        web.addJavascriptInterface(BuyBridge(), "AndroidBuy")

        // Inject the WKWebView-style bridge shim before page scripts run.
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            WebViewCompat.addDocumentStartJavaScript(web, BRIDGE_SHIM, setOf("*"))
        }

        web.webViewClient = object : WebViewClient() {
            override fun onPageStarted(v: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                super.onPageStarted(v, url, favicon)
                // Fallback shim injection when DOCUMENT_START_SCRIPT is unavailable.
                if (!WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                    v?.evaluateJavascript(BRIDGE_SHIM, null)
                }
            }

            override fun shouldOverrideUrlLoading(
                v: WebView, request: WebResourceRequest
            ): Boolean = handleNavigation(request.url)
        }

        // target=_blank / window.open — resolve the url and open it externally,
        // keeping the app WebView on the app origin.
        web.webChromeClient = object : WebChromeClient() {
            override fun onCreateWindow(
                v: WebView, isDialog: Boolean, isUserGesture: Boolean, resultMsg: android.os.Message
            ): Boolean {
                val href = v.hitTestResult.extra
                if (href != null) {
                    handleNavigation(Uri.parse(href))
                }
                return false
            }
        }

        // Song downloads: navigator.share({files}) is unsupported in WebView, so
        // the web save button falls through to a real download of
        // /download/<jid>.mp3 — catch it and hand it to DownloadManager.
        web.setDownloadListener { url, userAgent, contentDisposition, mimetype, _ ->
            downloadSong(url, userAgent, contentDisposition, mimetype)
        }
    }

    /**
     * Keep app-origin http(s) navigation in the WebView; send everything else
     * (external sites, checkout urls, mailto:, tel:, custom schemes) out to the
     * system — Custom Tabs for web urls, ACTION_VIEW for the rest.
     */
    private fun handleNavigation(uri: Uri): Boolean {
        val scheme = uri.scheme?.lowercase()
        val host = uri.host
        if (scheme == "http" || scheme == "https") {
            if (host != null && host.equals(baseHost, ignoreCase = true)) {
                return false // same origin: let the WebView load it
            }
            return try {
                CustomTabsIntent.Builder().build().launchUrl(this, uri)
                true
            } catch (e: Exception) {
                openExternally(uri)
            }
        }
        // Non-web schemes (mailto, tel, intent, market, etc.)
        return openExternally(uri)
    }

    private fun openExternally(uri: Uri): Boolean {
        return try {
            startActivity(Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            true
        } catch (e: Exception) {
            Log.w(TAG, "no handler for $uri", e)
            false
        }
    }

    private fun downloadSong(
        url: String, userAgent: String?, contentDisposition: String?, mimetype: String?
    ) {
        try {
            val cookies = CookieManager.getInstance().getCookie(url)
            val fileName = URLUtil.guessFileName(url, contentDisposition, mimetype ?: "audio/mpeg")
            val request = DownloadManager.Request(Uri.parse(url)).apply {
                setMimeType(mimetype ?: "audio/mpeg")
                if (!cookies.isNullOrEmpty()) addRequestHeader("Cookie", cookies)
                if (!userAgent.isNullOrEmpty()) addRequestHeader("User-Agent", userAgent)
                setDescription(getString(R.string.downloading_song))
                setTitle(fileName)
                setNotificationVisibility(
                    DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED
                )
                // Scoped-storage-safe public dir; DownloadManager owns the write.
                setDestinationInExternalPublicDir(Environment.DIRECTORY_MUSIC, fileName)
            }
            val dm = getSystemService(DOWNLOAD_SERVICE) as DownloadManager
            dm.enqueue(request)
            Toast.makeText(this, getString(R.string.downloading_song), Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            Log.e(TAG, "download failed", e)
            Toast.makeText(this, "Couldn't save song", Toast.LENGTH_SHORT).show()
        }
    }

    override fun onDestroy() {
        io.shutdownNow()
        super.onDestroy()
    }

    // Support hardware/system back inside the web history.
    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (this::web.isInitialized && web.canGoBack()) {
            web.goBack()
        } else {
            @Suppress("DEPRECATION")
            super.onBackPressed()
        }
    }

    /**
     * STUB purchase bridge. The web page calls buy('songs30'); on iOS this runs
     * the StoreKit consumable com.nicedreamz.ownatune.songs30 ($2.99, "30 Songs")
     * and POSTs the signed receipt to /api/iap_verify.
     *
     * TODO(billing): implement Google Play Billing (BillingClient) for a matching
     * consumable SKU, then POST the Play purchase token to a server-side
     * /api/iap_verify (Play Developer API verification, out of scope for this
     * client repo). For now we surface a non-blocking notice.
     */
    inner class BuyBridge {
        @JavascriptInterface
        fun buy(pack: String) {
            Log.i(TAG, "buy() requested for pack=$pack (billing not yet implemented)")
            runOnUiThread {
                Toast.makeText(
                    this@MainActivity,
                    getString(R.string.purchases_coming_soon),
                    Toast.LENGTH_LONG
                ).show()
            }
        }
    }
}
