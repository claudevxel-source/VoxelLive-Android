package com.voxellive.android;
import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;
public class MainActivity extends Activity {
    private WebView webView;
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        webView = new WebView(this);
        WebSettings settings = webView.getSettings(); settings.setJavaScriptEnabled(true); settings.setDomStorageEnabled(true); settings.setAllowFileAccess(false);
        webView.setWebViewClient(new WebViewClient()); webView.setWebChromeClient(new WebChromeClient()); setContentView(webView);
        try { startForegroundService(new Intent(this, VoxelLiveService.class)); }
        catch (Exception e) { Toast.makeText(this, "Gagal menjalankan VoxelLive: " + e.getMessage(), Toast.LENGTH_LONG).show(); }
        webView.postDelayed(() -> webView.loadUrl("http://127.0.0.1:8766/"), 2200);
    }
    @Override public void onBackPressed() { if (webView != null && webView.canGoBack()) webView.goBack(); else super.onBackPressed(); }
}
