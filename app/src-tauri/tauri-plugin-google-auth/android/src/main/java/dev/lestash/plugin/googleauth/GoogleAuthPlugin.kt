package dev.lestash.plugin.googleauth

import android.app.Activity
import app.tauri.annotation.Command
import app.tauri.annotation.InvokeArg
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.JSObject
import app.tauri.plugin.Plugin

@InvokeArg
class AuthorizeArgs {
    lateinit var scopes: Array<String>
    lateinit var webClientId: String
}

@TauriPlugin
class GoogleAuthPlugin(private val activity: Activity) : Plugin(activity) {

    @Command
    fun authorize(invoke: Invoke) {
        // Scaffold only — the AuthorizationClient flow is wired in a follow-up commit.
        // Returning an explicit error here keeps the Rust ↔ Kotlin contract testable
        // before the real flow lands.
        invoke.reject("google-auth: authorize() not yet implemented")
    }

    @Suppress("unused")
    private fun ok(invoke: Invoke, serverAuthCode: String, grantedScopes: List<String>) {
        val payload = JSObject()
        payload.put("serverAuthCode", serverAuthCode)
        payload.put("grantedScopes", grantedScopes)
        invoke.resolve(payload)
    }
}
