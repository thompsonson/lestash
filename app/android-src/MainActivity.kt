package dev.lestash.app

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebView
import org.json.JSONObject
import java.io.File

class MainActivity : TauriActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleShareIntent(intent)
        setupWebViewPermissions()
    }

    private fun setupWebViewPermissions() {
        val rootView = findViewById<android.view.View>(android.R.id.content) ?: return
        rootView.viewTreeObserver.addOnGlobalLayoutListener(object :
            android.view.ViewTreeObserver.OnGlobalLayoutListener {
            override fun onGlobalLayout() {
                val webView = findWebView(rootView) ?: return
                rootView.viewTreeObserver.removeOnGlobalLayoutListener(this)
                webView.webChromeClient = object : WebChromeClient() {
                    override fun onPermissionRequest(request: PermissionRequest) {
                        runOnUiThread { request.grant(request.resources) }
                    }
                }
            }
        })
    }

    private fun findWebView(view: android.view.View?): WebView? {
        if (view is WebView) return view
        if (view is android.view.ViewGroup) {
            for (i in 0 until view.childCount) {
                val found = findWebView(view.getChildAt(i))
                if (found != null) return found
            }
        }
        return null
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShareIntent(intent)
    }

    private fun handleShareIntent(intent: Intent) {
        val action = intent.action ?: return
        if (action != Intent.ACTION_SEND && action != Intent.ACTION_SEND_MULTIPLE) return

        val mimeType = intent.type ?: return
        val json = JSONObject()
        json.put("mimeType", mimeType)

        if (action == Intent.ACTION_SEND_MULTIPLE && mimeType.startsWith("image/")) {
            val uris = intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM) ?: return
            val filePaths = org.json.JSONArray()
            val fileNames = org.json.JSONArray()
            for (uri in uris) {
                val fileName = getFileName(uri) ?: "shared_image.jpg"
                val ext = fileName.substringAfterLast('.', "jpg")
                val tempFile = File(cacheDir, "shared_${System.currentTimeMillis()}_${filePaths.length()}.$ext")
                try {
                    contentResolver.openInputStream(uri)?.use { input ->
                        tempFile.outputStream().use { output -> input.copyTo(output) }
                    } ?: continue
                } catch (e: Exception) {
                    continue
                }
                filePaths.put(tempFile.absolutePath)
                fileNames.put(fileName)
            }
            if (filePaths.length() == 0) return
            json.put("filePaths", filePaths)
            json.put("fileNames", fileNames)
            val pendingFile = File(cacheDir, "pending_share.json")
            pendingFile.writeText(json.toString())
            return
        }

        if (mimeType.startsWith("audio/")) {
            val uri = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM) ?: return
            val fileName = getFileName(uri) ?: "shared_audio.m4a"
            val ext = fileName.substringAfterLast('.', "m4a")
            val tempFile = File(cacheDir, "shared_${System.currentTimeMillis()}.$ext")

            try {
                contentResolver.openInputStream(uri)?.use { input ->
                    tempFile.outputStream().use { output -> input.copyTo(output) }
                } ?: return
            } catch (e: Exception) {
                return
            }

            json.put("filePath", tempFile.absolutePath)
            json.put("fileName", fileName)
        } else if (mimeType.startsWith("image/")) {
            val uri = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM) ?: return
            val fileName = getFileName(uri) ?: "shared_image.jpg"
            val ext = fileName.substringAfterLast('.', "jpg")
            val tempFile = File(cacheDir, "shared_${System.currentTimeMillis()}.$ext")

            try {
                contentResolver.openInputStream(uri)?.use { input ->
                    tempFile.outputStream().use { output -> input.copyTo(output) }
                } ?: return
            } catch (e: Exception) {
                return
            }

            json.put("filePath", tempFile.absolutePath)
            json.put("fileName", fileName)
        } else if (mimeType == "text/html" || mimeType == "application/xhtml+xml") {
            // HTML file share: try file stream first, fall back to text
            val uri = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
            if (uri != null) {
                val fileName = getFileName(uri) ?: "shared_page.html"
                val tempFile = File(cacheDir, "shared_${System.currentTimeMillis()}.html")
                try {
                    contentResolver.openInputStream(uri)?.use { input ->
                        tempFile.outputStream().use { output -> input.copyTo(output) }
                    } ?: return
                } catch (e: Exception) {
                    return
                }
                json.put("filePath", tempFile.absolutePath)
                json.put("fileName", fileName)
            } else {
                val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return
                json.put("text", text)
                val subject = intent.getStringExtra(Intent.EXTRA_SUBJECT)
                if (subject != null) json.put("subject", subject)
            }
        } else if (mimeType.startsWith("text/")) {
            val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return
            json.put("text", text)
            val subject = intent.getStringExtra(Intent.EXTRA_SUBJECT)
            if (subject != null) json.put("subject", subject)
        } else {
            return
        }

        val pendingFile = File(cacheDir, "pending_share.json")
        pendingFile.writeText(json.toString())
    }

    private fun getFileName(uri: Uri): String? {
        if (uri.scheme == "content") {
            contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val idx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (idx >= 0) return cursor.getString(idx)
                }
            }
        }
        return uri.lastPathSegment
    }
}
