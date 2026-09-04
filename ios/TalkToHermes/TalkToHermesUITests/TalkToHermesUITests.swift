//
//  TalkToHermesUITests.swift
//  TalkToHermesUITests
//
//  Created by Ace on 29.08.26.
//

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
        let app = XCUIApplication()
        app.launchArguments += ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"]
        app.launch()

        let newConversationButton = app.buttons["Neue Unterhaltung"].firstMatch
        XCTAssertTrue(newConversationButton.waitForExistence(timeout: 3))
        newConversationButton.tap()

        XCTAssertTrue(app.staticTexts["Neue Unterhaltung starten?"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Der aktuell angezeigte Chat wird gelöscht."].exists)
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
            app.staticTexts["OpenCode Tool"].waitForExistence(timeout: 3),
            app.debugDescription
        )
        XCTAssertTrue(app.staticTexts["Tool-Aufruf 1"].exists)
        XCTAssertTrue(app.staticTexts["Projekt öffnen"].exists)
        XCTAssertTrue(app.staticTexts["Aufgerufen"].exists)
        XCTAssertTrue(app.staticTexts["Nicht erforderlich"].exists)
    }

    @MainActor
    func testOpensSecureConnectionSettings() throws {
        let app = XCUIApplication()
        app.launchArguments += ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"]
        app.launch()

        app.buttons["Einstellungen"].tap()

        XCTAssertTrue(app.navigationBars["Einstellungen"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.textFields["Server"].exists)
        XCTAssertTrue(app.textFields["ServerHostField"].exists)
        XCTAssertTrue(app.textFields["Port"].exists)
        XCTAssertEqual(app.textFields["Port"].value as? String, "8443")
        app.swipeUp()
        XCTAssertTrue(
            app.descendants(matching: .any)["ResponseStylePicker"].waitForExistence(timeout: 3)
        )
        XCTAssertTrue(
            app.descendants(matching: .any)["SpeechLanguagePicker"].waitForExistence(timeout: 3)
        )
        XCTAssertTrue(app.buttons["Sichern"].exists)
    }

    @MainActor
    func testLaunchPerformance() throws {
        // This measures how long it takes to launch your application.
        measure(metrics: [XCTApplicationLaunchMetric()]) {
            XCUIApplication().launch()
        }
    }
}
