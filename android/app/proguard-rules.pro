# Keep the JS bridge interface reachable from WebView JavaScript.
-keepclassmembers class com.nicedreamz.ownatune.** {
    @android.webkit.JavascriptInterface <methods>;
}
