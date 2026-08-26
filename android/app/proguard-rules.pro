# Song Forge / OwnATune -- R8 keep rules
#
# Google Play requires a minimum of 25% DEX coverage (optimization, shrinking,
# obfuscation) from February 2027. minifyEnabled false is 0%, so R8 has to be on.
#
# EVERYTHING BELOW EXISTS BECAUSE R8 BREAKS THIS APP AT RUNTIME, NOT AT BUILD TIME.
# The build will succeed either way. The app fails silently in the user's hands.

# --- the JavaScript bridge ---
# MainActivity does addJavascriptInterface(NativeBridge(), "SFAndroid"), and the web app
# calls SFAndroid.<method>() by name. R8 renames methods it thinks nothing calls, and it
# cannot see calls that live in JavaScript. Renaming these silently kills the bridge --
# which is how the app grants credits after a purchase.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
-keep class com.nicedreamz.ownatune.** { *; }

# --- Play Billing ---
# The library ships its own consumer rules, but this app takes money and a silent
# billing failure is the worst possible outcome. Belt and braces.
-keep class com.android.billingclient.api.** { *; }
-dontwarn com.android.billingclient.**

# --- Kotlin coroutines / reflection metadata ---
-keepclassmembers class kotlinx.coroutines.** { volatile <fields>; }
-keep class kotlin.Metadata { *; }
-dontwarn kotlinx.coroutines.**

# keep line numbers so a crash report is still readable after obfuscation
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
