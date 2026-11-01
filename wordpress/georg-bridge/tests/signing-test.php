<?php
// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Command-line tests for includes/signing.php: `php tests/signing-test.php`. Exits non-zero
 * on failure.
 *
 * @package GeorgBridge
 */

require __DIR__ . '/../includes/signing.php';

$georg_failures = 0;

/**
 * Records one check.
 *
 * @param bool   $ok   Whether it passed.
 * @param string $what Description.
 */
function georg_check( $ok, $what ) {
	global $georg_failures;
	echo ( $ok ? 'PASS ' : 'FAIL ' ) . $what . "\n";
	if ( ! $ok ) {
		++$georg_failures;
	}
}

/**
 * In-memory nonce store.
 *
 * @return callable
 */
function georg_memory_store() {
	$seen = array();
	return function ( $key_id, $nonce ) use ( &$seen ) {
		$id = $key_id . ':' . $nonce;
		if ( isset( $seen[ $id ] ) ) {
			return false;
		}
		$seen[ $id ] = true;
		return true;
	};
}

/**
 * Signs a request like the Georg client does.
 *
 * @param array $r    Request: key_id, timestamp, nonce, method, route, query, body.
 * @param array $keys Parsed keys.
 * @return array Lowercase headers.
 */
function georg_sign( $r, $keys ) {
	$sha = hash( 'sha256', $r['body'] );
	$msg = georg_bridge_sig_canonical_string( $r['key_id'], (string) $r['timestamp'], $r['nonce'], $r['method'], $r['route'], georg_bridge_sig_canonical_query( $r['query'] ), $sha );
	return array(
		'x-georg-key-id'         => $r['key_id'],
		'x-georg-timestamp'      => (string) $r['timestamp'],
		'x-georg-nonce'          => $r['nonce'],
		'x-georg-content-sha256' => $sha,
		'x-georg-signature'      => hash_hmac( 'sha256', $msg, $keys[ $r['key_id'] ] ),
	);
}

/**
 * Verifies a request against headers.
 *
 * @param array    $headers Headers.
 * @param array    $r       Request as received.
 * @param array    $keys    Parsed keys.
 * @param callable $store   Nonce store.
 * @param int|null $now     Server time; defaults to the request timestamp.
 * @return array
 */
function georg_verify( $headers, $r, $keys, $store, $now = null ) {
	return georg_bridge_sig_verify( $headers, $r['method'], $r['route'], $r['query'], $r['body'], $keys, null === $now ? (int) $r['timestamp'] : $now, $store );
}

// Shared vectors.
$georg_data = json_decode( file_get_contents( __DIR__ . '/vectors.json' ), true ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_get_contents_file_get_contents
foreach ( $georg_data['vectors'] as $v ) {
	$keys  = georg_bridge_sig_parse_keys( array( $v['key_id'] => $v['secret'] ) );
	$body  = base64_decode( $v['body_base64'] ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_decode
	$canon = georg_bridge_sig_canonical_string( $v['key_id'], (string) $v['timestamp'], $v['nonce'], $v['method'], $v['route'], georg_bridge_sig_canonical_query( $v['query'] ), hash( 'sha256', $body ) );
	georg_check( hash( 'sha256', $body ) === $v['content_sha256'], "vector {$v['name']}: body hash" );
	georg_check( $canon === $v['canonical'], "vector {$v['name']}: canonical string" );
	georg_check( hash_hmac( 'sha256', $canon, $keys[ $v['key_id'] ] ) === $v['signature'], "vector {$v['name']}: signature" );
	$headers = array(
		'x-georg-key-id'         => $v['key_id'],
		'x-georg-timestamp'      => (string) $v['timestamp'],
		'x-georg-nonce'          => $v['nonce'],
		'x-georg-content-sha256' => $v['content_sha256'],
		'x-georg-signature'      => $v['signature'],
	);
	$result  = georg_bridge_sig_verify( $headers, $v['method'], $v['route'], $v['query'], $body, $keys, $v['timestamp'], georg_memory_store() );
	georg_check( true === $result['ok'], "vector {$v['name']}: verifies" );
}

// Key configuration.
$georg_secret_a = rtrim( strtr( base64_encode( str_repeat( 'a', 32 ) ), '+/', '-_' ), '=' );
$georg_secret_b = rtrim( strtr( base64_encode( str_repeat( 'b', 48 ) ), '+/', '-_' ), '=' );
$georg_keys     = georg_bridge_sig_parse_keys(
	array(
		'old' => $georg_secret_a,
		'new' => $georg_secret_b,
	)
);
georg_check( is_array( $georg_keys ) && 2 === count( $georg_keys ), 'two keys parse' );
georg_check( null === georg_bridge_sig_parse_keys( array( 'short' => rtrim( base64_encode( str_repeat( 'x', 31 ) ), '=' ) ) ), 'secret under 32 bytes rejected' );
georg_check( null === georg_bridge_sig_parse_keys( array( 'bad id!' => $georg_secret_a ) ), 'invalid key id rejected' );
georg_check( null === georg_bridge_sig_parse_keys( array( 'k' => 'not base64 *' ) ), 'non-base64url secret rejected' );
georg_check( null === georg_bridge_sig_parse_keys( array( 'k' => 123 ) ), 'non-string secret rejected' );
georg_check( null === georg_bridge_sig_parse_keys( array() ), 'empty key list rejected' );
georg_check( null === georg_bridge_sig_parse_keys( null ), 'missing key list rejected' );
georg_check( is_array( georg_bridge_sig_parse_keys( array( 'k' => base64_encode( str_repeat( 'a', 32 ) ) ) ) ), 'padded secret accepted' );
georg_check( null === georg_bridge_sig_parse_keys( array( 'k' => str_repeat( '=', 4 ) ) ), 'padding-only secret rejected' );

$georg_base = array(
	'key_id'    => 'new',
	'timestamp' => 1793570000,
	'nonce'     => '0123456789abcdef0123456789abcdef',
	'method'    => 'PUT',
	'route'     => '/georg/v1/sermons/georg:stpaul:sermon-1:website',
	'query'     => array( 'b' => '2', 'a' => 'x y' ),
	'body'      => '{"title":"Hello"}',
);

$georg_store   = georg_memory_store();
$georg_headers = georg_sign( $georg_base, $georg_keys );
$georg_result  = georg_verify( $georg_headers, $georg_base, $georg_keys, $georg_store );
georg_check( true === $georg_result['ok'] && 'new' === $georg_result['key_id'], 'valid request accepted' );
$georg_result = georg_verify( $georg_headers, $georg_base, $georg_keys, $georg_store );
georg_check( ! $georg_result['ok'] && 'georg_replayed' === $georg_result['code'] && 401 === $georg_result['status'], 'replayed nonce rejected' );

$georg_old = array_merge( $georg_base, array( 'key_id' => 'old' ) );
georg_check( georg_verify( georg_sign( $georg_old, $georg_keys ), $georg_old, $georg_keys, georg_memory_store() )['ok'], 'rotation: old key id accepted while configured' );
$georg_only_new = array( 'new' => $georg_keys['new'] );
$georg_result   = georg_verify( georg_sign( $georg_old, $georg_keys ), $georg_old, $georg_only_new, georg_memory_store() );
georg_check( ! $georg_result['ok'] && 'georg_invalid_signature' === $georg_result['code'], 'rotation: removed key id rejected' );

$georg_result = georg_verify( $georg_headers, $georg_base, $georg_keys, georg_memory_store(), $georg_base['timestamp'] + 301 );
georg_check( ! $georg_result['ok'] && 'georg_expired' === $georg_result['code'] && 401 === $georg_result['status'], 'expired timestamp rejected' );
$georg_result = georg_verify( $georg_headers, $georg_base, $georg_keys, georg_memory_store(), $georg_base['timestamp'] - 301 );
georg_check( ! $georg_result['ok'] && 'georg_expired' === $georg_result['code'], 'future timestamp rejected' );
georg_check( georg_verify( $georg_headers, $georg_base, $georg_keys, georg_memory_store(), $georg_base['timestamp'] + 300 )['ok'], 'timestamp at the edge of the window accepted' );

// Tampering with what was signed, after signing.
$georg_tampered = array(
	'body'   => array( 'body' => '{"title":"Hellp"}' ),
	'method' => array( 'method' => 'POST' ),
	'path'   => array( 'route' => '/georg/v1/sermons/georg:stpaul:sermon-2:website' ),
	'query'  => array( 'query' => array( 'b' => '3', 'a' => 'x y' ) ),
	'added query parameter' => array( 'query' => array( 'b' => '2', 'a' => 'x y', 'c' => '' ) ),
);
foreach ( $georg_tampered as $georg_what => $georg_change ) {
	$georg_result = georg_verify( $georg_headers, array_merge( $georg_base, $georg_change ), $georg_keys, georg_memory_store() );
	georg_check( ! $georg_result['ok'] && 401 === $georg_result['status'], "tampered $georg_what rejected" );
}
$georg_header_changes = array(
	'timestamp'   => array( 'x-georg-timestamp' => (string) ( $georg_base['timestamp'] + 1 ) ),
	'nonce'       => array( 'x-georg-nonce' => 'fedcba9876543210fedcba9876543210' ),
	'key id'      => array( 'x-georg-key-id' => 'old' ),
	'body hash'   => array( 'x-georg-content-sha256' => hash( 'sha256', 'other' ) ),
	'signature'   => array( 'x-georg-signature' => str_repeat( '0', 64 ) ),
	'unknown key' => array( 'x-georg-key-id' => 'gone' ),
);
foreach ( $georg_header_changes as $georg_what => $georg_change ) {
	$georg_result = georg_verify( array_merge( $georg_headers, $georg_change ), $georg_base, $georg_keys, georg_memory_store() );
	georg_check( ! $georg_result['ok'] && 'georg_invalid_signature' === $georg_result['code'] && 401 === $georg_result['status'], "tampered $georg_what header rejected" );
}

$georg_wrong_keys = georg_bridge_sig_parse_keys( array( 'new' => $georg_secret_a ) );
$georg_result     = georg_verify( georg_sign( $georg_base, $georg_wrong_keys ), $georg_base, $georg_keys, georg_memory_store() );
georg_check( ! $georg_result['ok'] && 'georg_invalid_signature' === $georg_result['code'], 'signature made with the wrong key rejected' );

$georg_malformed = array(
	'missing signature' => array( 'x-georg-signature' => null ),
	'missing nonce'     => array( 'x-georg-nonce' => null ),
	'missing timestamp' => array( 'x-georg-timestamp' => null ),
	'missing key id'    => array( 'x-georg-key-id' => null ),
	'missing body hash' => array( 'x-georg-content-sha256' => null ),
	'uppercase hex'     => array( 'x-georg-signature' => strtoupper( $georg_headers['x-georg-signature'] ) ),
	'short nonce'       => array( 'x-georg-nonce' => 'abc' ),
	'nonce with colon'  => array( 'x-georg-nonce' => str_repeat( 'a', 30 ) . ':b' ),
	'decimal timestamp' => array( 'x-georg-timestamp' => '1793570000.5' ),
	'negative time'     => array( 'x-georg-timestamp' => '-1793570000' ),
	'array header'      => array( 'x-georg-signature' => array( $georg_headers['x-georg-signature'] ) ),
);
foreach ( $georg_malformed as $georg_what => $georg_change ) {
	$georg_result = georg_verify( array_merge( $georg_headers, $georg_change ), $georg_base, $georg_keys, georg_memory_store() );
	georg_check( ! $georg_result['ok'] && 'georg_invalid_signature' === $georg_result['code'] && 401 === $georg_result['status'], "malformed headers ($georg_what) rejected" );
}
georg_check( ! georg_verify( $georg_headers, $georg_base, null, georg_memory_store() )['ok'], 'unconfigured keys reject everything' );

$georg_array_query = array_merge( $georg_base, array( 'query' => array( 'a' => array( '1', '2' ) ) ) );
$georg_result      = georg_verify( georg_sign( array_merge( $georg_base, array( 'query' => array() ) ), $georg_keys ), $georg_array_query, $georg_keys, georg_memory_store() );
georg_check( ! $georg_result['ok'] && 400 === $georg_result['status'], 'array query parameter rejected with 400' );

$georg_seen_bad = false;
$georg_spy      = function () use ( &$georg_seen_bad ) {
	$georg_seen_bad = true;
	return true;
};
georg_verify( array_merge( $georg_headers, array( 'x-georg-signature' => str_repeat( 'a', 64 ) ) ), $georg_base, $georg_keys, $georg_spy );
georg_check( ! $georg_seen_bad, 'nonce is not recorded for an invalid signature' );

georg_check( 'a=1&b=%20%2B~%2F' === georg_bridge_sig_canonical_query( array( 'b' => ' +~/', 'a' => '1', 'rest_route' => '/x' ) ), 'canonical query encodes like rawurlencode' );
georg_check( 'a=x%20y&b=2' === georg_bridge_sig_canonical_query( $georg_base['query'] ), 'canonical query sorts and encodes' );
georg_check( '' === georg_bridge_sig_canonical_query( array( 'rest_route' => '/georg/v1/audio' ) ), 'rest_route excluded' );

echo "\n" . $georg_failures . " failure(s)\n";
exit( $georg_failures ? 1 : 0 );
