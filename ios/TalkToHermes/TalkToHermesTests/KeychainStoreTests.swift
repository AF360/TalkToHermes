import Foundation
import Testing
@testable import TalkToHermes

struct KeychainStoreTests {
    @Test func rejectsEmptyToken() {
        let store = KeychainStore(service: "systems.acelab.TalkToHermes.tests.empty")

        #expect(throws: KeychainStoreError.emptyToken) {
            try store.saveToken("  \n")
        }
    }

    @Test func storesReadsAndDeletesTrimmedToken() throws {
        let store = KeychainStore(service: "systems.acelab.TalkToHermes.tests.\(UUID().uuidString)")

        try store.saveToken("  private-test-token\n")
        #expect(try store.readToken() == "private-test-token")

        try store.deleteToken()
        #expect(throws: KeychainStoreError.itemNotFound) {
            try store.readToken()
        }
    }
}
