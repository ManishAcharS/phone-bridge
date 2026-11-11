package dev.phonebridge.net

import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import android.util.Log

/**
 * The only place this app touches the user's storage, and it only ever touches
 * what the user picked.
 *
 * Rules, in the order they matter:
 *
 *  1. **Nothing is browsed.** The app never lists a directory the user did not
 *     choose. [destinationTree] is a URI granted by `ACTION_OPEN_DOCUMENT_TREE`,
 *     [outgoing] a list of URIs granted by `ACTION_OPEN_DOCUMENT`. Both are
 *     persisted with `takePersistableUriPermission`.
 *  2. **Nothing is written outside that tree.** Every received file is created as
 *     a child document of the chosen tree with a sanitised name; a name that
 *     would escape it (traversal, separator, reserved device name) is refused
 *     before any document exists.
 *  3. **The permission can be revoked at any time**, and revoking it makes the
 *     capability unavailable rather than causing a silent failure later.
 *
 * Storage Access Framework only: no `MANAGE_EXTERNAL_STORAGE`, no
 * `READ_EXTERNAL_STORAGE` and no path guessing. A phone that never picked a
 * folder simply cannot receive files, and says exactly that.
 */
object SafStore {

    private const val TAG = "PhoneBridgeSaf"

    const val PREFS = "phonebridge_saf"
    private const val KEY_TREE = "destination_tree"
    private const val KEY_LABEL = "destination_label"

    /** The intent the UI launches; kept here so the flags are not forgotten. */
    fun pickFolderIntent(): Intent =
        Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).addFlags(
            Intent.FLAG_GRANT_READ_URI_PERMISSION or
                Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION,
        )

    fun pickFilesIntent(): Intent =
        Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
            putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
            addFlags(
                Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION,
            )
        }

    /** Remember the folder the user picked. Returns the label shown in the UI. */
    fun rememberDestination(context: Context, tree: Uri): String {
        val label = displayName(context, tree) ?: tree.lastPathSegment ?: "chosen folder"
        context.contentResolver.takePersistableUriPermission(
            tree,
            Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
        )
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_TREE, tree.toString())
            .putString(KEY_LABEL, label)
            .apply()
        Log.i(TAG, "received folder set to '$label'")
        return label
    }

    /** Forget the folder: the next receive asks again rather than guessing. */
    fun forgetDestination(context: Context) {
        val stored = destinationTree(context)
        if (stored != null) {
            try {
                context.contentResolver.releasePersistableUriPermission(
                    stored,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
                )
            } catch (e: SecurityException) {
                Log.w(TAG, "could not release the folder permission: ${e.message}")
            }
        }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().remove(KEY_TREE).remove(KEY_LABEL).apply()
    }

    fun destinationTree(context: Context): Uri? {
        val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_TREE, null) ?: return null
        val uri = Uri.parse(raw)
        // A stored URI whose grant was revoked must stop counting as a destination.
        val held = context.contentResolver.persistedUriPermissions
        val usable = held.any { it.uri == uri && it.isWritePermission }
        if (!usable) {
            Log.w(TAG, "the saved folder is no longer granted; a new pick is required")
            return null
        }
        return uri
    }

    fun destinationLabel(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_LABEL, null) ?: ""

    /** True when receiving a file is possible right now. */
    fun canReceive(context: Context): Boolean = destinationTree(context) != null

    /**
     * Create (or replace) a child document [name] under the chosen tree.
     *
     * @throws StorageFailure with a sentence the user can act on.
     */
    fun createDocument(context: Context, name: String, mime: String): Uri {
        val tree = destinationTree(context)
            ?: throw StorageFailure(
                "storage_denied",
                "no folder has been chosen on the phone (Files card -> Choose folder)",
            )
        val documentId = DocumentsContract.getTreeDocumentId(tree)
        val parent = DocumentsContract.buildDocumentUriUsingTree(tree, documentId)
        return try {
            DocumentsContract.createDocument(context.contentResolver, parent, mime, name)
                ?: throw StorageFailure("storage_denied", "the phone would not create '$name'")
        } catch (e: SecurityException) {
            throw StorageFailure("storage_denied", "the folder permission was revoked: ${e.message}")
        } catch (e: Exception) {
            throw StorageFailure("storage_denied", "could not create '$name': ${e.message}")
        }
    }

    /** Delete a document this app created (a failed or cancelled `.part`). */
    fun deleteDocument(context: Context, uri: Uri): Boolean = try {
        DocumentsContract.deleteDocument(context.contentResolver, uri)
    } catch (e: Exception) {
        Log.w(TAG, "could not delete $uri: ${e.message}")
        false
    }

    /** Rename after a verified transfer: SAF's answer to an atomic rename. */
    fun renameDocument(context: Context, uri: Uri, name: String): Uri? = try {
        DocumentsContract.renameDocument(context.contentResolver, uri, name)
    } catch (e: Exception) {
        Log.w(TAG, "could not rename $uri: ${e.message}")
        null
    }

    fun displayName(context: Context, uri: Uri): String? {
        var cursor: Cursor? = null
        return try {
            cursor = context.contentResolver.query(uri, null, null, null, null)
            if (cursor != null && cursor.moveToFirst()) {
                val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (index >= 0 && !cursor.isNull(index)) cursor.getString(index) else null
            } else {
                null
            }
        } catch (e: Exception) {
            Log.w(TAG, "could not read the name of $uri: ${e.message}")
            null
        } finally {
            cursor?.close()
        }
    }

    fun sizeOf(context: Context, uri: Uri): Long {
        var cursor: Cursor? = null
        return try {
            cursor = context.contentResolver.query(uri, null, null, null, null)
            if (cursor != null && cursor.moveToFirst()) {
                val index = cursor.getColumnIndex(OpenableColumns.SIZE)
                if (index >= 0 && !cursor.isNull(index)) cursor.getLong(index) else -1L
            } else {
                -1L
            }
        } catch (e: Exception) {
            Log.w(TAG, "could not read the size of $uri: ${e.message}")
            -1L
        } finally {
            cursor?.close()
        }
    }

    /** MIME type for a name, defaulting to binary so nothing is opened as text. */
    fun mimeFor(name: String): String {
        val extension = name.substringAfterLast('.', "").lowercase()
        return when (extension) {
            "txt", "log", "md", "json", "csv" -> "text/plain"
            "jpg", "jpeg" -> "image/jpeg"
            "png" -> "image/png"
            "pdf" -> "application/pdf"
            "zip" -> "application/zip"
            "mp4" -> "video/mp4"
            "mp3" -> "audio/mpeg"
            else -> "application/octet-stream"
        }
    }

    /** Ask the resolver whether a document can actually be written. */
    fun canWrite(context: Context, uri: Uri): Boolean = try {
        context.contentResolver.openOutputStream(uri, "wa")?.use { true } ?: false
    } catch (e: Exception) {
        Log.w(TAG, "cannot write $uri: ${e.javaClass.simpleName}: ${e.message}")
        false
    }

    /** The content resolver, kept behind a helper so the engine does not repeat it. */
    fun resolver(context: Context): ContentResolver = context.contentResolver

    /** The intent the Files card launches for the folder picker. */
    const val REQUEST_FOLDER = 0x5F01
    const val REQUEST_FILES = 0x5F02
}

/** A storage refusal, reported to the PC as `storage_denied`. */
class StorageFailure(code: String, message: String) : RpcFailure(code, message)
