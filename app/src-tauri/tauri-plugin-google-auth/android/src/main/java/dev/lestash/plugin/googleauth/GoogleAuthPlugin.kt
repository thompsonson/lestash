package dev.lestash.plugin.googleauth

import android.app.Activity
import android.util.Log
import androidx.activity.result.ActivityResult
import androidx.activity.result.IntentSenderRequest
import app.tauri.annotation.ActivityCallback
import app.tauri.annotation.Command
import app.tauri.annotation.InvokeArg
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.JSObject
import app.tauri.plugin.Plugin
import com.google.android.gms.auth.api.identity.AuthorizationRequest
import com.google.android.gms.auth.api.identity.AuthorizationResult
import com.google.android.gms.auth.api.identity.Identity
import com.google.android.gms.common.api.Scope

@InvokeArg
class AuthorizeArgs {
    lateinit var scopes: Array<String>
    lateinit var webClientId: String
}

@TauriPlugin
class GoogleAuthPlugin(private val activity: Activity) : Plugin(activity) {

    @Command
    fun authorize(invoke: Invoke) {
        val args = try {
            invoke.parseArgs(AuthorizeArgs::class.java)
        } catch (e: Exception) {
            invoke.reject("invalid arguments: ${e.message}")
            return
        }

        if (args.scopes.isEmpty()) {
            invoke.reject("at least one scope is required")
            return
        }
        if (args.webClientId.isBlank()) {
            invoke.reject("webClientId is required for offline access")
            return
        }

        val request = AuthorizationRequest.builder()
            .setRequestedScopes(args.scopes.map { Scope(it) })
            .requestOfflineAccess(args.webClientId)
            .build()

        Identity.getAuthorizationClient(activity)
            .authorize(request)
            .addOnSuccessListener { result: AuthorizationResult ->
                val pending = result.pendingIntent
                if (pending != null) {
                    val req = IntentSenderRequest.Builder(pending.intentSender).build()
                    startIntentSenderForResult(invoke, req, "onAuthorizationResult")
                } else {
                    resolveSuccess(invoke, result)
                }
            }
            .addOnFailureListener { e ->
                Log.e(TAG, "AuthorizationClient.authorize failed", e)
                invoke.reject("authorization failed: ${e.message}")
            }
    }

    @ActivityCallback
    fun onAuthorizationResult(invoke: Invoke, result: ActivityResult) {
        if (result.resultCode != Activity.RESULT_OK) {
            invoke.reject("authorization cancelled")
            return
        }
        val data = result.data
        if (data == null) {
            invoke.reject("authorization returned no data")
            return
        }
        try {
            val authResult = Identity.getAuthorizationClient(activity)
                .getAuthorizationResultFromIntent(data)
            resolveSuccess(invoke, authResult)
        } catch (e: Exception) {
            Log.e(TAG, "getAuthorizationResultFromIntent failed", e)
            invoke.reject("authorization result error: ${e.message}")
        }
    }

    private fun resolveSuccess(invoke: Invoke, result: AuthorizationResult) {
        val code = result.serverAuthCode
        if (code.isNullOrBlank()) {
            invoke.reject(
                "no server auth code returned " +
                    "(verify the Web OAuth client id passed via webClientId)"
            )
            return
        }
        val payload = JSObject()
        payload.put("serverAuthCode", code)
        payload.put("grantedScopes", result.grantedScopes ?: emptyList<String>())
        invoke.resolve(payload)
    }

    private companion object {
        const val TAG = "GoogleAuthPlugin"
    }
}
