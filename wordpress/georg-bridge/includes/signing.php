<?php
// SPDX-License-Identifier: AGPL-3.0-only
/**
 * HMAC-SHA256 request signing (docs/wordpress.md). Pure PHP with no WordPress calls, so
 * tests/signing-test.php can run it from the command line.
 *
 * @package GeorgBridge
 */

if ( ! defined( 'ABSPATH' ) && 'cli' !== PHP_SAPI ) {
	exit;
}

define( 'GEORG_BRIDGE_SIG_ALGORITHM', 'GEORG-HMAC-SHA256' );
define( 'GEORG_BRIDGE_SIG_WINDOW', 300 );
define( 'GEORG_BRIDGE_SIG_MIN_SECRET_BYTES', 32 );
define( 'GEORG_BRIDGE_SIG_KEY_ID_PATTERN', '/^[A-Za-z0-9._-]{1,64}$/' );
define( 'GEORG_BRIDGE_SIG_NONCE_PATTERN', '/^[A-Za-z0-9_-]{22,128}$/' );
define( 'GEORG_BRIDGE_SIG_HEX64_PATTERN', '/^[0-9a-f]{64}$/' );
define( 'GEORG_BRIDGE_SIG_TIMESTAMP_PATTERN', '/^[0-9]{1,12}$/' );

/**
 * Decodes the GEORG_BRIDGE_KEYS setting.
 *
 * @param mixed $config Array of key id => base64url secret (padding optional).
 * @return array|null Key id => raw secret bytes, or null if the setting is missing or any
 *                    entry is invalid (bad id, not base64url, or under 32 bytes).
 */
function georg_bridge_sig_parse_keys( $config ) {
	if ( ! is_array( $config ) || ! $config ) {
		return null;
	}
	$keys = array();
	foreach ( $config as $id => $encoded ) {
		$id = (string) $id;
		if ( ! preg_match( GEORG_BRIDGE_SIG_KEY_ID_PATTERN, $id ) || ! is_string( $encoded ) ) {
			return null;
		}
		$encoded = trim( $encoded );
		if ( ! preg_match( '/^[A-Za-z0-9_-]+={0,2}$/', $encoded ) ) {
			return null;
		}
		$plain  = rtrim( $encoded, '=' );
		$secret = base64_decode( strtr( $plain, '-_', '+/' ) . str_repeat( '=', ( 4 - strlen( $plain ) % 4 ) % 4 ), true ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_decode
		if ( false === $secret || strlen( $secret ) < GEORG_BRIDGE_SIG_MIN_SECRET_BYTES ) {
			return null;
		}
		$keys[ $id ] = $secret;
	}
	return $keys;
}

/**
 * Canonical query: rawurlencode(k)=rawurlencode(v) pairs sorted by key then value, joined
 * by "&", without `rest_route`.
 *
 * @param array $query Decoded query parameters.
 * @return string|null Null if a value is not a scalar.
 */
function georg_bridge_sig_canonical_query( $query ) {
	$pairs = array();
	foreach ( (array) $query as $key => $value ) {
		$key = (string) $key;
		if ( 'rest_route' === $key ) {
			continue;
		}
		if ( ! is_scalar( $value ) ) {
			return null;
		}
		$pairs[] = array( $key, (string) $value );
	}
	usort(
		$pairs,
		function ( $a, $b ) {
			$by_key = strcmp( $a[0], $b[0] );
			return 0 !== $by_key ? $by_key : strcmp( $a[1], $b[1] );
		}
	);
	$parts = array();
	foreach ( $pairs as $pair ) {
		$parts[] = rawurlencode( $pair[0] ) . '=' . rawurlencode( $pair[1] );
	}
	return implode( '&', $parts );
}

/**
 * The string that is signed.
 *
 * @param string $key_id          Key id.
 * @param string $timestamp       Unix seconds as sent.
 * @param string $nonce           Nonce.
 * @param string $method          HTTP method.
 * @param string $route           REST route, e.g. "/georg/v1/audio".
 * @param string $canonical_query Output of georg_bridge_sig_canonical_query().
 * @param string $body_sha256     Lowercase hex SHA-256 of the raw body.
 * @return string
 */
function georg_bridge_sig_canonical_string( $key_id, $timestamp, $nonce, $method, $route, $canonical_query, $body_sha256 ) {
	return implode(
		"\n",
		array( GEORG_BRIDGE_SIG_ALGORITHM, $key_id, $timestamp, $nonce, strtoupper( $method ), $route, $canonical_query, $body_sha256 )
	);
}

/**
 * Verifies a signed request.
 *
 * @param array    $headers     Lowercase header name => value (missing headers absent or null).
 * @param string   $method      HTTP method.
 * @param string   $route       REST route.
 * @param array    $query       Decoded query parameters.
 * @param string   $body        Raw request body.
 * @param array    $keys        Output of georg_bridge_sig_parse_keys().
 * @param int      $now         Current Unix time.
 * @param callable $nonce_store Called as ( $key_id, $nonce, $now ) only for requests with a
 *                              valid signature; returns true if the nonce was not seen before
 *                              and is now recorded.
 * @param int      $window      Accepted clock difference in seconds.
 * @return array 'ok' => true and 'key_id', or 'ok' => false with 'status', 'code', 'message'.
 */
function georg_bridge_sig_verify( $headers, $method, $route, $query, $body, $keys, $now, $nonce_store, $window = GEORG_BRIDGE_SIG_WINDOW ) {
	$get = function ( $name ) use ( $headers ) {
		return isset( $headers[ $name ] ) && is_string( $headers[ $name ] ) ? $headers[ $name ] : '';
	};
	$key_id    = $get( 'x-georg-key-id' );
	$timestamp = $get( 'x-georg-timestamp' );
	$nonce     = $get( 'x-georg-nonce' );
	$body_hash = $get( 'x-georg-content-sha256' );
	$signature = $get( 'x-georg-signature' );

	$invalid = array(
		'ok'      => false,
		'status'  => 401,
		'code'    => 'georg_invalid_signature',
		'message' => 'invalid signature',
	);
	if ( ! preg_match( GEORG_BRIDGE_SIG_KEY_ID_PATTERN, $key_id )
		|| ! preg_match( GEORG_BRIDGE_SIG_TIMESTAMP_PATTERN, $timestamp )
		|| ! preg_match( GEORG_BRIDGE_SIG_NONCE_PATTERN, $nonce )
		|| ! preg_match( GEORG_BRIDGE_SIG_HEX64_PATTERN, $body_hash )
		|| ! preg_match( GEORG_BRIDGE_SIG_HEX64_PATTERN, $signature )
		|| ! is_array( $keys )
		|| ! isset( $keys[ $key_id ] ) ) {
		return $invalid;
	}
	if ( abs( (int) $now - (int) $timestamp ) > $window ) {
		return array(
			'ok'      => false,
			'status'  => 401,
			'code'    => 'georg_expired',
			'message' => 'request expired',
		);
	}
	if ( ! hash_equals( hash( 'sha256', (string) $body ), $body_hash ) ) {
		return $invalid;
	}
	$canonical_query = georg_bridge_sig_canonical_query( $query );
	if ( null === $canonical_query ) {
		return array(
			'ok'      => false,
			'status'  => 400,
			'code'    => 'georg_bad_query',
			'message' => 'query parameters must be single values',
		);
	}
	$message  = georg_bridge_sig_canonical_string( $key_id, $timestamp, $nonce, $method, $route, $canonical_query, $body_hash );
	$expected = hash_hmac( 'sha256', $message, $keys[ $key_id ] );
	if ( ! hash_equals( $expected, $signature ) ) {
		return $invalid;
	}
	if ( true !== call_user_func( $nonce_store, $key_id, $nonce, (int) $now ) ) {
		return array(
			'ok'      => false,
			'status'  => 401,
			'code'    => 'georg_replayed',
			'message' => 'replayed request',
		);
	}
	return array(
		'ok'     => true,
		'key_id' => $key_id,
	);
}
