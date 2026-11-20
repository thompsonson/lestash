package dev.lestash.app

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import org.json.JSONObject
import java.io.File

class MainActivity : TauriActivity() {

    companion object {
        /** Suffixes the server imports as text (lestash.core.text_files.FORMATS). */
        private val TEXT_SUFFIXES = listOf(".txt", ".md", ".markdown")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleShareIntent(intent)
        // The WebView keeps wry's own chrome client: it opens file pickers for
        // <input type="file"> and grants the microphone. Replacing it, as this did
        // before #328, left every file input dead.
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
        } else if (mimeType == "application/pdf") {
            val uri = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM) ?: return
            val fileName = getFileName(uri) ?: "shared.pdf"
            val tempFile = File(cacheDir, "shared_${System.currentTimeMillis()}.pdf")
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
            // A text or Markdown file comes as a stream; a selection or link as text (#389)
            val uri = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
            if (uri != null) {
                if (!putTextFile(uri, mimeType, json)) return
            } else {
                val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return
                json.put("text", text)
                val subject = intent.getStringExtra(Intent.EXTRA_SUBJECT)
                if (subject != null) json.put("subject", subject)
            }
        } else {
            // Fallback: check file extension for generic MIME types (e.g. application/octet-stream)
            val uri = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
            if (uri != null) {
                val fileName = getFileName(uri) ?: ""
                if (fileName.endsWith(".html", ignoreCase = true) || fileName.endsWith(".htm", ignoreCase = true)) {
                    val tempFile = File(cacheDir, "shared_${System.currentTimeMillis()}.html")
                    try {
                        contentResolver.openInputStream(uri)?.use { input ->
                            tempFile.outputStream().use { output -> input.copyTo(output) }
                        } ?: return
                    } catch (e: Exception) {
                        return
                    }
                    json.put("mimeType", "text/html")
                    json.put("filePath", tempFile.absolutePath)
                    json.put("fileName", fileName)
                } else if (fileName.endsWith(".pdf", ignoreCase = true)) {
                    val tempFile = File(cacheDir, "shared_${System.currentTimeMillis()}.pdf")
                    try {
                        contentResolver.openInputStream(uri)?.use { input ->
                            tempFile.outputStream().use { output -> input.copyTo(output) }
                        } ?: return
                    } catch (e: Exception) {
                        return
                    }
                    json.put("mimeType", "application/pdf")
                    json.put("filePath", tempFile.absolutePath)
                    json.put("fileName", fileName)
                } else if (TEXT_SUFFIXES.any { fileName.endsWith(it, ignoreCase = true) }) {
                    if (!putTextFile(uri, mimeType, json)) return
                } else {
                    return
                }
            } else {
                return
            }
        }

        val pendingFile = File(cacheDir, "pending_share.json")
        pendingFile.writeText(json.toString())
    }

    /**
     * Copy a shared text or Markdown file to the cache and describe it in [json].
     * The name keeps a suffix the server imports (.txt, .md, .markdown), so a file
     * named without one still imports. Returns false if the file can't be read.
     */
    private fun putTextFile(uri: Uri, mimeType: String, json: JSONObject): Boolean {
        var fileName = getFileName(uri) ?: "shared"
        if (TEXT_SUFFIXES.none { fileName.endsWith(it, ignoreCase = true) }) {
            fileName += if (mimeType.contains("markdown")) ".md" else ".txt"
        }
        val markdown = !fileName.endsWith(".txt", ignoreCase = true)
        val ext = if (markdown) "md" else "txt"
        val tempFile = File(cacheDir, "shared_${System.currentTimeMillis()}.$ext")
        try {
            contentResolver.openInputStream(uri)?.use { input ->
                tempFile.outputStream().use { output -> input.copyTo(output) }
            } ?: return false
        } catch (e: Exception) {
            return false
        }
        json.put("mimeType", if (markdown) "text/markdown" else "text/plain")
        json.put("filePath", tempFile.absolutePath)
        json.put("fileName", fileName)
        return true
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
