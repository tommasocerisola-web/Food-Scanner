import org.gradle.api.tasks.Sync

plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

val repoRoot = rootProject.projectDir.parentFile
val stagedAssets = layout.buildDirectory.dir("generated/foodscanner-assets")
val stageFoodScannerAssets = tasks.register<Sync>("stageFoodScannerAssets") {
    into(stagedAssets.map { it.dir("foodscanner") })
    from(File(repoRoot, "engine")) {
        into("engine")
        exclude("__pycache__/**", "**/*.pyc")
    }
    from(File(repoRoot, "ui")) {
        into("ui")
        exclude("__pycache__/**", "**/*.pyc")
    }
    from(File(repoRoot, "data")) {
        into("data")
    }
}

android {
    namespace = "com.foodscanner.app"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.foodscanner.app"
        minSdk = 26
        targetSdk = 37
        versionCode = 3
        versionName = "0.3-beta"

        // L'APK è pensato per telefoni Android 17 arm64.
        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        getByName("debug") {
            isMinifyEnabled = false
        }
        getByName("release") {
            isMinifyEnabled = false
            // Per un APK installabile senza un keystore personale.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    sourceSets.getByName("main") {
        assets.srcDir(stagedAssets)
    }
}

chaquopy {
    defaultConfig {
        version = "3.13"
        pyc {
            src = false
        }
    }
}

tasks.matching { it.name.startsWith("merge") && it.name.endsWith("Assets") }.configureEach {
    dependsOn(stageFoodScannerAssets)
}
tasks.named("preBuild").configure {
    dependsOn(stageFoodScannerAssets)
}

dependencies {
    implementation("androidx.core:core:1.13.1")
}
