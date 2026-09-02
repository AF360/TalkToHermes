import Foundation
import Security

nonisolated enum KeychainStoreError: Error, Equatable, LocalizedError {
    case emptyToken
    case itemNotFound
    case invalidData
    case unhandledStatus(OSStatus)

    var errorDescription: String? {
        switch self {
        case .emptyToken:
            String(localized: "Der App-Token darf nicht leer sein.")
        case .itemNotFound:
            String(localized: "Es ist kein App-Token im Schlüsselbund gespeichert.")
        case .invalidData:
            String(localized: "Der gespeicherte App-Token konnte nicht gelesen werden.")
        case .unhandledStatus:
            String(localized: "Der App-Token konnte im Schlüsselbund nicht verarbeitet werden.")
        }
    }
}

nonisolated struct KeychainStore: Sendable {
    static let appTokenAccount = "app-token"

    let service: String

    init(service: String = "systems.acelab.TalkToHermes") {
        self.service = service
    }

    func saveToken(_ token: String, account: String = Self.appTokenAccount) throws {
        let normalized = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else {
            throw KeychainStoreError.emptyToken
        }

        let query = baseQuery(account: account)
        let data = Data(normalized.utf8)
        let existingStatus = SecItemCopyMatching(query as CFDictionary, nil)
        let status: OSStatus
        if existingStatus == errSecSuccess {
            status = SecItemUpdate(
                query as CFDictionary,
                [
                    kSecValueData as String: data,
                    kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
                ] as CFDictionary
            )
        } else if existingStatus == errSecItemNotFound {
            var item = query
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            status = SecItemAdd(item as CFDictionary, nil)
        } else {
            throw KeychainStoreError.unhandledStatus(existingStatus)
        }
        guard status == errSecSuccess else {
            throw KeychainStoreError.unhandledStatus(status)
        }
    }

    func readToken(account: String = Self.appTokenAccount) throws -> String {
        var query = baseQuery(account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound {
            throw KeychainStoreError.itemNotFound
        }
        guard status == errSecSuccess else {
            throw KeychainStoreError.unhandledStatus(status)
        }
        guard
            let data = item as? Data,
            let token = String(data: data, encoding: .utf8),
            !token.isEmpty
        else {
            throw KeychainStoreError.invalidData
        }
        return token
    }

    func deleteToken(account: String = Self.appTokenAccount) throws {
        let status = SecItemDelete(baseQuery(account: account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainStoreError.unhandledStatus(status)
        }
    }

    private func baseQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}
