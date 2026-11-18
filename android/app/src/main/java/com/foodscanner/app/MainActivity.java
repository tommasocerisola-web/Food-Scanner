package com.foodscanner.app;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.provider.MediaStore;
import android.util.Log;
import android.view.View;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.core.content.FileProvider;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

public class MainActivity extends Activity {
    private static final String TAG = "FoodScanner";
    private static final String SERVER_HOST = "127.0.0.1";
    private static final int SERVER_PORT = 8765;
    private static final int FILE_CHOOSER_REQUEST = 1001;

    private WebView webView;
    private ProgressBar progress;
    private TextView errorView;
    private ValueCallback<Uri[]> filePathCallback;
    private Uri cameraOutputUri;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        setContentView(R.layout.activity_main);

        FrameLayout root = findViewById(R.id.root);
        ViewCompat.setOnApplyWindowInsetsListener(root, (view, insets) -> {
            Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemBars());
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return insets;
        });

        webView = findViewById(R.id.webview);
        progress = findViewById(R.id.progress);
        errorView = findViewById(R.id.error);
        setupWebView();
        startPythonServer();
    }

    private void setupWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setSupportZoom(false);
        webView.setBackgroundColor(0xFF0B100E);
        if (BuildConfig.DEBUG) {
            WebView.setWebContentsDebuggingEnabled(true);
        }

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String host = uri.getHost();
                if ("127.0.0.1".equals(host) || "localhost".equals(host)) {
                    return false;
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                } catch (ActivityNotFoundException ignored) {
                    Toast.makeText(MainActivity.this, "No app available to open the link.", Toast.LENGTH_SHORT).show();
                }
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                progress.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    showStartupError(new RuntimeException(error.getDescription().toString()));
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(
                    WebView view,
                    ValueCallback<Uri[]> callback,
                    FileChooserParams params
            ) {
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                return openPhotoChooser();
            }
        });
    }

    private boolean openPhotoChooser() {
        try {
            File cameraDir = new File(getCacheDir(), "camera");
            if (!cameraDir.exists() && !cameraDir.mkdirs()) {
                throw new IOException("Cannot create the photo folder");
            }
            File photo = File.createTempFile("foodscanner-", ".jpg", cameraDir);
            cameraOutputUri = FileProvider.getUriForFile(
                    this,
                    getPackageName() + ".fileprovider",
                    photo
            );

            Intent capture = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
            capture.putExtra(MediaStore.EXTRA_OUTPUT, cameraOutputUri);
            capture.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);

            Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
            pick.addCategory(Intent.CATEGORY_OPENABLE);
            pick.setType("image/*");

            Intent chooser = Intent.createChooser(pick, "Choose a photo");
            chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[]{capture});
            startActivityForResult(chooser, FILE_CHOOSER_REQUEST);
            return true;
        } catch (Exception error) {
            Log.e(TAG, "Failed to open camera/gallery", error);
            if (filePathCallback != null) {
                filePathCallback.onReceiveValue(null);
                filePathCallback = null;
            }
            Toast.makeText(this, "Cannot open the camera or the gallery.", Toast.LENGTH_SHORT).show();
            return false;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == FILE_CHOOSER_REQUEST) {
            Uri[] result = null;
            if (resultCode == RESULT_OK) {
                if (data != null && data.getData() != null) {
                    result = new Uri[]{data.getData()};
                } else if (cameraOutputUri != null) {
                    result = new Uri[]{cameraOutputUri};
                }
            }
            if (filePathCallback != null) {
                filePathCallback.onReceiveValue(result);
                filePathCallback = null;
            }
            cameraOutputUri = null;
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    private void startPythonServer() {
        progress.setVisibility(View.VISIBLE);
        new Thread(() -> {
            try {
                File appDir = new File(getFilesDir(), "foodscanner");
                syncAssets(appDir);

                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(getApplicationContext()));
                }
                Python.getInstance()
                        .getModule("bootstrap")
                        .callAttr("start", appDir.getAbsolutePath(), SERVER_PORT);

                runOnUiThread(() -> {
                    errorView.setVisibility(View.GONE);
                    webView.loadUrl("http://" + SERVER_HOST + ":" + SERVER_PORT + "/");
                });
            } catch (Throwable error) {
                Log.e(TAG, "Python server startup failed", error);
                runOnUiThread(() -> showStartupError(error));
            }
        }, "foodscanner-start").start();
    }

    private void showStartupError(Throwable error) {
        progress.setVisibility(View.GONE);
        errorView.setVisibility(View.VISIBLE);
        errorView.setText(
                "Food Scanner could not start the Python engine.\n\n"
                        + error.getClass().getSimpleName() + ": " + safeMessage(error)
                        + "\n\nClose and reopen the app. If the problem persists, reinstall the APK."
        );
    }

    private String safeMessage(Throwable error) {
        String message = error.getMessage();
        return message == null || message.isEmpty() ? "unknown error" : message;
    }

    private void syncAssets(File appDir) throws IOException {
        SharedPreferences prefs = getSharedPreferences("foodscanner_assets", MODE_PRIVATE);
        String current = prefs.getString("version", "");
        if (appDir.isDirectory() && BuildConfig.VERSION_NAME.equals(current)) {
            return;
        }
        if (appDir.exists() && !deleteTree(appDir)) {
            throw new IOException("Cannot update the app assets");
        }
        copyAssetTree("foodscanner", appDir);
        prefs.edit().putString("version", BuildConfig.VERSION_NAME).commit();
    }

    private boolean deleteTree(File file) {
        if (file.isDirectory()) {
            File[] children = file.listFiles();
            if (children != null) {
                for (File child : children) {
                    if (!deleteTree(child)) {
                        return false;
                    }
                }
            }
        }
        return file.delete();
    }

    private void copyAssetTree(String assetPath, File target) throws IOException {
        String[] children = getAssets().list(assetPath);
        if (children != null && children.length > 0) {
            if (!target.exists() && !target.mkdirs()) {
                throw new IOException("Cannot create " + target);
            }
            for (String child : children) {
                copyAssetTree(assetPath + "/" + child, new File(target, child));
            }
            return;
        }

        File parent = target.getParentFile();
        if (parent != null && !parent.exists() && !parent.mkdirs()) {
            throw new IOException("Cannot create " + parent);
        }
        try (InputStream input = getAssets().open(assetPath);
             OutputStream output = new FileOutputStream(target)) {
            byte[] buffer = new byte[8192];
            int count;
            while ((count = input.read(buffer)) != -1) {
                output.write(buffer, 0, count);
            }
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (filePathCallback != null) {
            filePathCallback.onReceiveValue(null);
            filePathCallback = null;
        }
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }
}
