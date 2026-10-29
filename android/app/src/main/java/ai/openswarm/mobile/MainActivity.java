package ai.openswarm.mobile;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.speech.RecognizerIntent;
import android.view.Window;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.util.ArrayList;

/** Standalone OpenSwarm for Android:
 *  - Starts an on-device OpenCode Server + local workspace engine via Bridge
 *  - Configures WebView with universal access from file URLs so both Chromium BoringSSL fetch()
 *    and Native.http() work seamlessly without CORS barriers. */
public class MainActivity extends Activity {
    static final int REQ_VOICE = 4711;
    private WebView webView;
    private Bridge bridge;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        bridge = new Bridge(this);
        webView = new WebView(this);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        s.setAllowFileAccessFromFileURLs(true);
        s.setAllowUniversalAccessFromFileURLs(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient());
        webView.addJavascriptInterface(bridge, "Native");
        webView.loadUrl("file:///android_asset/index.html");
        setContentView(webView);
    }

    void js(final String code) {
        runOnUiThread(() -> {
            if (webView != null) webView.evaluateJavascript(code, null);
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_VOICE && resultCode == RESULT_OK && data != null) {
            ArrayList<String> r = data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS);
            if (r != null && !r.isEmpty()) js("window.__voice(" + JSONObject.quote(r.get(0)) + ")");
        }
    }

    @Override
    public void onBackPressed() {
        js("window.__back && window.__back()");
    }

    @Override
    protected void onDestroy() {
        if (bridge != null) bridge.stopServer();
        super.onDestroy();
    }
}
