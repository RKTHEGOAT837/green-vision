package app.greenvision;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.GeolocationPermissions;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import androidx.activity.OnBackPressedCallback;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

/**
 * Green Vision, as an Android app.
 *
 * The studio is one HTML file and the engine is Python, and a phone will not
 * run Python. So the APK does not try: scripts/build_static.py bakes the
 * trained engine down to JSON - the ranking, the canopy forecast, the soil
 * table, the species KB, per city - and those files ship inside the APK. The
 * page already prefers a live engine and falls back to those exports, so the
 * same index.html runs here unmodified.
 *
 * What that buys, and what it costs, stated plainly because the app says the
 * same thing to its user:
 *
 *   Works with no connection at all - the planting priority ranking for five
 *   cities, the score decomposition, the worklist with its quantities and
 *   costs and its CSV export, the canopy watch list, the species picks, the
 *   design studio and its 25-year projection. All of that is arithmetic over
 *   files that are already on the phone.
 *
 *   Needs a connection - the basemap tiles, which are Esri's and are not ours
 *   to bundle, and the live air-quality and weather readings for a point you
 *   tap. Without a connection the map is blank tiles and the area panel says
 *   the reading did not load, which is the honest thing for it to say.
 *
 * The WebView is deliberately plain. No JavaScript bridge is installed:
 * addJavascriptInterface is the standard way to turn a WebView into a remote
 * code execution surface, and nothing here needs it.
 */
public class MainActivity extends AppCompatActivity {

    private WebView web;
    private FrameLayout fullscreenHost;
    private View fullscreenView;
    private WebChromeClient.CustomViewCallback fullscreenCallback;

    /** The baked bundle, copied into assets/ at build time by gradle. */
    private static final String HOME = "file:///android_asset/www/index.html";

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        web = findViewById(R.id.web);
        fullscreenHost = findViewById(R.id.fullscreen_host);

        // Keep the page clear of the status bar and the gesture bar. Leaflet
        // sizes to its container, so letting the WebView run under the system
        // bars would put the zoom controls under the navigation pill.
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.root), (v, insets) -> {
            Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemBars());
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return WindowInsetsCompat.CONSUMED;
        });

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // the worklist remembers its assumptions
        s.setDatabaseEnabled(true);
        s.setGeolocationEnabled(true);         // "where am I" centres the map
        s.setSupportZoom(false);               // the map does its own zooming
        s.setBuiltInZoomControls(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        // The page is local; the tiles and the weather API are not. Without
        // this, an https:// fetch from a file:// document is blocked as mixed
        // content and the map silently stays blank.
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);

        // Deliberately NOT enabled: setAllowFileAccessFromFileURLs and
        // setAllowUniversalAccessFromFileURLs. The page reads its baked JSON
        // through fetch() on relative paths under android_asset, which the
        // asset loader below serves same-origin, so neither is needed - and
        // both would let any script in the page read the device filesystem.
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);

        CookieManager.getInstance().setAcceptCookie(true);
        web.setBackgroundColor(Color.parseColor("#0a1512"));

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                String scheme = u.getScheme() == null ? "" : u.getScheme();
                // Keep our own pages inside the app; hand anything else to the
                // browser rather than rendering a foreign site chromeless and
                // indistinguishable from our own UI.
                if (scheme.equals("file")) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));
                } catch (ActivityNotFoundException e) {
                    Toast.makeText(MainActivity.this,
                            R.string.no_app_for_link, Toast.LENGTH_SHORT).show();
                }
                return true;
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin,
                                                           GeolocationPermissions.Callback cb) {
                // Our own page only. The system location permission is asked
                // for separately by the OS, so this is not a second grant.
                cb.invoke(origin, origin != null && origin.startsWith("file://"), false);
            }

            @Override
            public void onPermissionRequest(PermissionRequest request) {
                request.deny();     // no camera, no microphone: nothing needs them
            }

            @Override
            public void onShowCustomView(View view, CustomViewCallback callback) {
                if (fullscreenView != null) { callback.onCustomViewHidden(); return; }
                fullscreenView = view;
                fullscreenCallback = callback;
                fullscreenHost.addView(view, new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT));
                fullscreenHost.setVisibility(View.VISIBLE);
                web.setVisibility(View.GONE);
            }

            @Override
            public void onHideCustomView() {
                if (fullscreenView == null) return;
                fullscreenHost.removeView(fullscreenView);
                fullscreenHost.setVisibility(View.GONE);
                web.setVisibility(View.VISIBLE);
                fullscreenView = null;
                if (fullscreenCallback != null) {
                    fullscreenCallback.onCustomViewHidden();
                    fullscreenCallback = null;
                }
            }
        });

        // Back should walk the page's own history before it leaves the app.
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                if (fullscreenView != null) {
                    web.getWebChromeClient().onHideCustomView();
                } else if (web.canGoBack()) {
                    web.goBack();
                } else {
                    setEnabled(false);
                    getOnBackPressedDispatcher().onBackPressed();
                }
            }
        });

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(HOME);
        }
    }

    /** Survive a rotation without throwing the reader back to the splash. */
    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onPause() {
        super.onPause();
        web.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            ((ViewGroup) web.getParent()).removeView(web);
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
