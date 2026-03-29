package dev.lestash.app

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import org.json.JSONObject
import java.io.File

class MainActivity : TauriActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleShareIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShareIntent(intent)
    }

    private fun handleShareIntent(intent: Intent) {
        if (intent.action != Intent.ACTION_SEND) return

        val mimeType = intent.type ?: return
        val json = JSONObject()
        json.put("mimeType", mimeType)

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
