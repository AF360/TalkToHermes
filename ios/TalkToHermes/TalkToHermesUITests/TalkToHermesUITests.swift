//
//  TalkToHermesUITests.swift
//  TalkToHermesUITests
//
//  Created by Ace on 29.08.26.
//

import UIKit
import XCTest

final class TalkToHermesUITests: XCTestCase {

    override func setUpWithError() throws {
        // Put setup code here. This method is called before the invocation of each test method in the class.

        // In UI tests it is usually best to stop immediately when a failure occurs.
        continueAfterFailure = false

        // In UI tests it’s important to set the initial state - such as interface orientation - required for your tests before they run. The setUp method is a good place to do this.
    }

    override func tearDownWithError() throws {
        // Put teardown code here. This method is called after the invocation of each test method in the class.
    }

    @MainActor
    func testShowsVoiceHomeScreen() throws {
        let app = XCUIApplication()
        app.launchArguments += ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"]
        app.launch()

        XCTAssertTrue(app.staticTexts["TalkToHermes"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["Einstellungen"].exists)
        XCTAssertTrue(app.buttons["Sprechen"].exists)
        XCTAssertTrue(
            app.staticTexts.matching(
                NSPredicate(
                    format: "label BEGINSWITH %@",
                    "Knopf nicht gedrückt halten: einmal zum Starten tippen, einmal zum Senden."
                )
            ).firstMatch.exists
        )
        XCTAssertTrue(app.staticTexts["ConnectionStatus"].exists)
        XCTAssertFalse(app.buttons["Sprechen"].isEnabled)
    }

    @MainActor
    func testCompactBrandHeaderStartsCloseToToolbarAndStaysPinned() throws {
        XCUIDevice.shared.orientation = .portrait
        try XCTSkipUnless(
            UIDevice.current.userInterfaceIdiom == .phone,
            "Compact header geometry applies to iPhone."
        )
        let app = XCUIApplication()
        app.launchArguments += [
            "-AppleLanguages", "(de)", "-AppleLocale", "de_DE",
            "--ui-test-tool-activity",
        ]
        app.launch()

        let title = app.staticTexts["BrandTitle"]
        let settings = app.buttons["Einstellungen"]
        XCTAssertTrue(title.waitForExistence(timeout: 3), app.debugDescription)
        XCTAssertTrue(settings.exists)
        let scrollingTitle = app.staticTexts["VoiceStageTitle"]
        XCTAssertTrue(scrollingTitle.exists, app.debugDescription)
        let initialY = title.frame.minY
        let initialScrollingY = scrollingTitle.frame.minY
        XCTAssertLessThanOrEqual(initialY - settings.frame.maxY, 12)

        app.scrollViews.firstMatch.swipeUp()
        XCTAssertLessThan(scrollingTitle.frame.minY, initialScrollingY - 2)
        XCTAssertEqual(title.frame.minY, initialY, accuracy: 2)
    }

    @MainActor
    func testShowsEnglishVoiceHomeScreen() throws {
        let app = XCUIApplication()
        app.launchArguments += ["-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
        app.launch()

        XCTAssertTrue(app.staticTexts["TalkToHermes"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["Settings"].exists)
        XCTAssertTrue(app.buttons["Speak"].exists)
        XCTAssertTrue(app.staticTexts["Speak naturally"].exists)
        XCTAssertTrue(
            app.staticTexts.matching(
                NSPredicate(format: "label BEGINSWITH %@", "There is no need to hold a button:")
            ).firstMatch.exists
        )
    }

    @MainActor
    func testConfirmsBeforeStartingNewConversation() throws {
        XCUIDevice.shared.orientation = .portrait
        let app = XCUIApplication()
        app.launchArguments += ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"]
        app.launch()

        let newConversationButton = app.buttons["Neue Unterhaltung"].firstMatch
        XCTAssertTrue(newConversationButton.waitForExistence(timeout: 3))
        newConversationButton.tap()

        XCTAssertTrue(
            app.descendants(matching: .any)["Neue Unterhaltung starten?"].waitForExistence(timeout: 3),
            app.debugDescription
        )
        XCTAssertTrue(
            app.descendants(matching: .any)["Der aktuell angezeigte Chat wird gelöscht."].exists
        )
        XCTAssertTrue(app.buttons["Abbrechen"].exists)
        XCTAssertTrue(app.buttons["Neue Unterhaltung"].exists)

        app.buttons["Abbrechen"].tap()
        XCTAssertFalse(app.staticTexts["Neue Unterhaltung starten?"].exists)
        XCTAssertTrue(newConversationButton.exists)
    }

    @MainActor
    func testShowsOneMarkerPerToolCallAndOpensSafeDetails() throws {
        XCUIDevice.shared.orientation = .portrait
        let app = XCUIApplication()
        app.launchArguments += [
            "-AppleLanguages", "(de)", "-AppleLocale", "de_DE",
            "--ui-test-tool-activity",
        ]
        app.launch()
        app.swipeUp()

        let firstCall = app.buttons["ToolInvocation-ui-tool-turn-tool-6"]
        let secondCall = app.buttons["ToolInvocation-ui-tool-turn-tool-7"]
        XCTAssertTrue(
            firstCall.waitForExistence(timeout: 3),
            app.debugDescription
        )
        XCTAssertTrue(secondCall.exists)

        firstCall.tap()

        XCTAssertTrue(
            app.staticTexts["Read File"].waitForExistence(timeout: 3),
            app.debugDescription
        )
        XCTAssertTrue(app.staticTexts["Tool-Aufruf 1"].exists)
        XCTAssertTrue(app.staticTexts["Datei gelesen"].exists)
        XCTAssertTrue(app.staticTexts["Aufgerufen"].exists)
        XCTAssertTrue(app.staticTexts["Nicht erforderlich"].exists)
    }

    @MainActor
    func testOpensSecureConnectionSettings() throws {
        XCUIDevice.shared.orientation = .portrait
        let app = XCUIApplication()
        app.launchArguments += ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"]
        app.launch()

        let settingsButton = app.buttons["SettingsButton"]
        XCTAssertTrue(settingsButton.waitForExistence(timeout: 3), app.debugDescription)
        XCTAssertEqual(settingsButton.frame.width, 44, accuracy: 0.5)
        XCTAssertEqual(settingsButton.frame.height, 44, accuracy: 0.5)
        settingsButton.tap()

        XCTAssertTrue(app.navigationBars["Einstellungen"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.textFields["Server"].exists)
        XCTAssertTrue(app.textFields["ServerHostField"].exists)
        XCTAssertTrue(app.textFields["Port"].exists)
        XCTAssertEqual(app.textFields["Port"].value as? String, "8443")
        let form = app.collectionViews.firstMatch
        XCTAssertTrue(form.exists, app.debugDescription)
        let responseStylePicker = app.descendants(matching: .any)["ResponseStylePicker"]
        for _ in 0..<5 where !responseStylePicker.isHittable {
            form.swipeUp()
        }
        XCTAssertTrue(
            responseStylePicker.isHittable,
            app.debugDescription
        )
        let speechLanguagePicker = app.descendants(matching: .any)["SpeechLanguagePicker"]
        for _ in 0..<5 where !speechLanguagePicker.isHittable {
            form.swipeUp()
        }
        XCTAssertTrue(speechLanguagePicker.isHittable, app.debugDescription)

        let colorThemePicker = app.descendants(matching: .any)["ColorThemePicker"]
        for _ in 0..<5 where !colorThemePicker.isHittable {
            form.swipeUp()
        }
        XCTAssertTrue(colorThemePicker.isHittable, app.debugDescription)
        XCTAssertTrue(app.buttons["Sichern"].exists)
    }

    @MainActor
    func testCompactBottomBarRespectsVisibleBoundsAfterScrolling() throws {
        XCUIDevice.shared.orientation = .portrait
        try XCTSkipUnless(
            UIDevice.current.userInterfaceIdiom == .phone,
            "Compact bottom-bar geometry applies to iPhone."
        )
        let app = XCUIApplication()
        app.launchArguments += [
            "-AppleLanguages", "(de)", "-AppleLocale", "de_DE",
            "--ui-test-tool-activity",
        ]
        app.launch()

        let recordButton = app.buttons["Sprechen"]
        let actionLabel = app.staticTexts["PrimaryActionLabel"]
        XCTAssertTrue(recordButton.waitForExistence(timeout: 3), app.debugDescription)
        XCTAssertTrue(actionLabel.exists, app.debugDescription)
        XCTAssertLessThan(recordButton.frame.maxY, actionLabel.frame.minY)
        XCTAssertGreaterThanOrEqual(
            app.frame.maxY - actionLabel.frame.maxY,
            try bottomSafeAreaInset(in: app) - 0.5
        )
        let scrollingTitle = app.staticTexts["VoiceStageTitle"]
        XCTAssertTrue(scrollingTitle.exists, app.debugDescription)
        let initialScrollingY = scrollingTitle.frame.minY

        let initial = XCTAttachment(screenshot: app.screenshot())
        initial.name = "Compact bottom bar — first presentation"
        initial.lifetime = .keepAlways
        add(initial)

        app.scrollViews.firstMatch.swipeUp()
        XCTAssertLessThan(scrollingTitle.frame.minY, initialScrollingY - 2)
        XCTAssertGreaterThanOrEqual(
            app.frame.maxY - actionLabel.frame.maxY,
            try bottomSafeAreaInset(in: app) - 0.5
        )
        let scrolled = XCTAttachment(screenshot: app.screenshot())
        scrolled.name = "Compact bottom bar — scrolled conversation"
        scrolled.lifetime = .keepAlways
        add(scrolled)
    }

    @MainActor
    func testWideBottomBarRespectsVisibleBoundsInLandscape() throws {
        XCUIDevice.shared.orientation = .landscapeRight
        let app = XCUIApplication()
        app.launchArguments += [
            "-AppleLanguages", "(de)", "-AppleLocale", "de_DE",
            "--ui-test-tool-activity",
        ]
        app.launch()

        let recordButton = app.buttons["Sprechen"]
        let actionLabel = app.staticTexts["PrimaryActionLabel"]
        XCTAssertTrue(recordButton.waitForExistence(timeout: 3), app.debugDescription)
        XCTAssertTrue(actionLabel.exists, app.debugDescription)
        XCTAssertLessThan(recordButton.frame.maxY, actionLabel.frame.minY)
        XCTAssertGreaterThanOrEqual(
            app.frame.maxY - actionLabel.frame.maxY,
            try bottomSafeAreaInset(in: app) - 0.5
        )

        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Wide bottom bar — landscape"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    @MainActor
    private func bottomSafeAreaInset(in app: XCUIApplication) throws -> CGFloat {
        let guide = app.descendants(matching: .any)["BottomSafeAreaInset"]
        XCTAssertTrue(guide.waitForExistence(timeout: 3), app.debugDescription)
        let rawValue = try XCTUnwrap(guide.value as? String)
        let inset = CGFloat(try XCTUnwrap(Double(rawValue)))
        XCTAssertGreaterThan(inset, 0)
        return inset
    }

    @MainActor
    func testLaunchPerformance() throws {
        // This measures how long it takes to launch your application.
        measure(metrics: [XCTApplicationLaunchMetric()]) {
            XCUIApplication().launch()
        }
    }
}
