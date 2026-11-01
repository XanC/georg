<?php
// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Plugin Name:       Georg Bridge
 * Plugin URI:        https://github.com/XanC/georg
 * Description:       REST endpoints that let Georg upload sermon audio and create or update Church Content sermons.
 * Version:           0.2.1
 * Requires at least: 5.6
 * Requires PHP:      7.4
 * License:           AGPL-3.0-only
 * License URI:       https://www.gnu.org/licenses/agpl-3.0.html
 * Text Domain:       georg-bridge
 *
 * @package GeorgBridge
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

require_once __DIR__ . '/includes/signing.php';

define( 'GEORG_BRIDGE_NAMESPACE', 'georg/v1' );
define( 'GEORG_BRIDGE_KEY_META', '_georg_publication_key' );
define( 'GEORG_BRIDGE_SHA256_META', '_georg_sha256' );
define( 'GEORG_BRIDGE_POST_TYPE', 'ctc_sermon' );
define( 'GEORG_BRIDGE_AUDIO_META', '_ctc_sermon_audio' );
define( 'GEORG_BRIDGE_UPLOAD_SUBDIR', 'sermons' );
define( 'GEORG_BRIDGE_KEY_PATTERN', '[A-Za-z0-9:._-]{1,190}' );
define( 'GEORG_BRIDGE_LOCK_SECONDS', 300 );
define( 'GEORG_BRIDGE_NONCE_PREFIX', '_georg_nonce_' );
define( 'GEORG_BRIDGE_NONCE_PURGE_LIMIT', 100 );
define(
	'GEORG_BRIDGE_SIGNED_HEADERS',
	array( 'x-georg-key-id', 'x-georg-timestamp', 'x-georg-nonce', 'x-georg-content-sha256', 'x-georg-signature' )
);

add_action( 'rest_api_init', 'georg_bridge_register_routes' );

/**
 * Registers the georg/v1 routes.
 */
function georg_bridge_register_routes() {
	$key_arg = array(
		'publication_key' => array(
			'type'              => 'string',
			'required'          => true,
			'validate_callback' => 'georg_bridge_valid_key',
		),
	);

	register_rest_route(
		GEORG_BRIDGE_NAMESPACE,
		'/audio',
		array(
			'methods'             => 'POST',
			'callback'            => 'georg_bridge_upload_audio',
			'permission_callback' => 'georg_bridge_permission_audio',
			'args'                => array(
				'publication_key' => array(
					'type'              => 'string',
					'required'          => true,
					'validate_callback' => 'georg_bridge_valid_key',
				),
				'filename'        => array(
					'type'     => 'string',
					'required' => false,
				),
				'title'           => array(
					'type'     => 'string',
					'required' => false,
				),
			),
		)
	);

	register_rest_route(
		GEORG_BRIDGE_NAMESPACE,
		'/sermons/(?P<publication_key>' . GEORG_BRIDGE_KEY_PATTERN . ')',
		array(
			array(
				'methods'             => 'GET',
				'callback'            => 'georg_bridge_get_sermon',
				'permission_callback' => 'georg_bridge_permission_sermons',
				'args'                => $key_arg,
			),
			array(
				'methods'             => 'PUT',
				'callback'            => 'georg_bridge_put_sermon',
				'permission_callback' => 'georg_bridge_permission_sermons',
				'args'                => array_merge(
					$key_arg,
					array(
						'title'               => array(
							'type'     => 'string',
							'required' => true,
						),
						'content'             => array(
							'type'     => 'string',
							'required' => false,
						),
						'status'              => array(
							'type'     => 'string',
							'enum'     => array( 'draft', 'private', 'publish' ),
							'default'  => 'draft',
							'required' => false,
						),
						'date'                => array(
							'type'     => 'string',
							'format'   => 'date-time',
							'required' => false,
						),
						'date_gmt'            => array(
							'type'     => 'string',
							'format'   => 'date-time',
							'required' => false,
						),
						'terms'               => array(
							'type'     => 'object',
							'required' => false,
						),
						'audio_attachment_id' => array(
							'type'     => 'integer',
							'required' => false,
						),
					)
				),
			),
		)
	);
}

/**
 * Whether a publication key has the accepted form.
 *
 * @param mixed $value Key from the request.
 * @return bool
 */
function georg_bridge_valid_key( $value ) {
	return is_string( $value ) && 1 === preg_match( '/^' . GEORG_BRIDGE_KEY_PATTERN . '$/', $value );
}

/**
 * Error returned for every configuration problem, without detail.
 *
 * @return WP_Error
 */
function georg_bridge_not_configured() {
	return new WP_Error( 'georg_not_configured', __( 'georg-bridge is not configured.', 'georg-bridge' ), array( 'status' => 503 ) );
}

/**
 * Signing keys from the GEORG_BRIDGE_KEYS constant (wp-config.php).
 *
 * @return array|null Key id => secret bytes, or null if missing or invalid.
 */
function georg_bridge_keys() {
	return defined( 'GEORG_BRIDGE_KEYS' ) ? georg_bridge_sig_parse_keys( GEORG_BRIDGE_KEYS ) : null;
}

/**
 * Records a nonce atomically; false if it was already used.
 *
 * @param string $key_id Key id.
 * @param string $nonce  Nonce.
 * @param int    $now    Unix time.
 * @return bool
 */
function georg_bridge_record_nonce( $key_id, $nonce, $now ) {
	global $wpdb;
	$name = GEORG_BRIDGE_NONCE_PREFIX . hash( 'sha256', $key_id . ':' . $nonce );
	// phpcs:ignore WordPress.DB.DirectDatabaseQuery
	$rows = $wpdb->query( $wpdb->prepare( "INSERT IGNORE INTO {$wpdb->options} (option_name, option_value, autoload) VALUES (%s, %s, 'off')", $name, (string) $now ) );
	return 1 === $rows;
}

/**
 * Deletes nonces old enough that their timestamps would be rejected anyway.
 *
 * @param int $now Unix time.
 */
function georg_bridge_purge_nonces( $now ) {
	global $wpdb;
	// phpcs:ignore WordPress.DB.DirectDatabaseQuery
	$wpdb->query(
		$wpdb->prepare(
			"DELETE FROM {$wpdb->options} WHERE option_name LIKE %s AND CAST(option_value AS UNSIGNED) < %d LIMIT %d",
			$wpdb->esc_like( GEORG_BRIDGE_NONCE_PREFIX ) . '%',
			$now - 2 * GEORG_BRIDGE_SIG_WINDOW,
			GEORG_BRIDGE_NONCE_PURGE_LIMIT
		)
	);
}

/**
 * Authenticates a georg/v1 request by its HMAC signature and switches to the configured user.
 *
 * Only these routes call it, so the key never authenticates other WordPress endpoints.
 *
 * @param WP_REST_Request $request Request.
 * @return true|WP_Error
 */
function georg_bridge_authenticate( $request ) {
	if ( ! is_ssl() && ! ( defined( 'GEORG_BRIDGE_ALLOW_HTTP' ) && GEORG_BRIDGE_ALLOW_HTTP ) ) {
		return new WP_Error( 'georg_https_required', __( 'georg-bridge requires HTTPS.', 'georg-bridge' ), array( 'status' => 403 ) );
	}
	$keys = georg_bridge_keys();
	if ( null === $keys ) {
		return georg_bridge_not_configured();
	}
	$body   = (string) $request->get_body();
	$length = isset( $_SERVER['CONTENT_LENGTH'] ) ? (int) $_SERVER['CONTENT_LENGTH'] : 0;
	if ( '' === $body && $length > 0 ) {
		// PHP discards bodies larger than post_max_size.
		/* translators: %s: PHP post_max_size setting */
		return new WP_Error( 'georg_too_large', sprintf( __( 'The request is larger than PHP post_max_size (%s).', 'georg-bridge' ), ini_get( 'post_max_size' ) ), array( 'status' => 413 ) );
	}
	$headers = array();
	foreach ( GEORG_BRIDGE_SIGNED_HEADERS as $name ) {
		$headers[ $name ] = $request->get_header( $name );
	}
	$now    = time();
	$result = georg_bridge_sig_verify( $headers, $request->get_method(), $request->get_route(), $request->get_query_params(), $body, $keys, $now, 'georg_bridge_record_nonce' );
	if ( ! $result['ok'] ) {
		return new WP_Error( $result['code'], $result['message'], array( 'status' => $result['status'] ) );
	}
	georg_bridge_purge_nonces( $now );
	$user = defined( 'GEORG_BRIDGE_USER' ) && is_string( GEORG_BRIDGE_USER ) ? get_user_by( 'login', GEORG_BRIDGE_USER ) : false;
	if ( ! $user ) {
		return georg_bridge_not_configured();
	}
	wp_set_current_user( $user->ID );
	return true;
}

/**
 * Authenticates the request, then checks the configured user's capabilities.
 *
 * @param WP_REST_Request $request      Request.
 * @param string[]        $capabilities Capabilities the operation needs.
 * @return true|WP_Error
 */
function georg_bridge_permission( $request, $capabilities ) {
	$auth = georg_bridge_authenticate( $request );
	if ( is_wp_error( $auth ) ) {
		return $auth;
	}
	$allowed = true;
	foreach ( $capabilities as $capability ) {
		$allowed = $allowed && current_user_can( $capability );
	}

	/**
	 * Filters whether the configured user may perform this georg-bridge operation.
	 *
	 * Runs only after the request signature has been verified.
	 *
	 * @param bool            $allowed Default decision.
	 * @param WP_REST_Request $request Request.
	 */
	$allowed = (bool) apply_filters( 'georg_bridge_permission', $allowed, $request );
	if ( $allowed ) {
		return true;
	}
	return new WP_Error(
		'georg_forbidden',
		__( 'The georg-bridge user lacks the capabilities for this operation.', 'georg-bridge' ),
		array( 'status' => 403 )
	);
}

/**
 * Permission for the sermon routes.
 *
 * @param WP_REST_Request $request Request.
 * @return true|WP_Error
 */
function georg_bridge_permission_sermons( $request ) {
	return georg_bridge_permission( $request, array( 'edit_others_posts' ) );
}

/**
 * Permission for the audio upload route.
 *
 * @param WP_REST_Request $request Request.
 * @return true|WP_Error
 */
function georg_bridge_permission_audio( $request ) {
	return georg_bridge_permission( $request, array( 'edit_others_posts', 'upload_files' ) );
}

/**
 * Takes a short-lived per-key lock so concurrent retries cannot create duplicates.
 *
 * @param string $scope Lock scope ("audio" or "sermon").
 * @param string $key   Publication key.
 * @return string|WP_Error Option name holding the lock, or an error if it is held.
 */
function georg_bridge_lock( $scope, $key ) {
	$name = 'georg_bridge_lock_' . $scope . '_' . md5( $key );
	if ( add_option( $name, time(), '', 'no' ) ) {
		return $name;
	}
	$since = (int) get_option( $name );
	if ( $since > 0 && time() - $since > GEORG_BRIDGE_LOCK_SECONDS ) {
		update_option( $name, time(), false );
		return $name;
	}
	return new WP_Error(
		'georg_busy',
		__( 'Another request for this publication key is in progress.', 'georg-bridge' ),
		array( 'status' => 409 )
	);
}

/**
 * Returns the first post of a type carrying the publication key, or null.
 *
 * @param string   $post_type Post type.
 * @param string   $key       Publication key.
 * @param string[] $statuses  Post statuses to search.
 * @return WP_Post|null
 */
function georg_bridge_find( $post_type, $key, $statuses ) {
	$posts = get_posts(
		array(
			'post_type'        => $post_type,
			'post_status'      => $statuses,
			'meta_key'         => GEORG_BRIDGE_KEY_META, // phpcs:ignore WordPress.DB.SlowDBQuery.slow_db_query_meta_key
			'meta_value'       => $key, // phpcs:ignore WordPress.DB.SlowDBQuery.slow_db_query_meta_value
			'posts_per_page'   => 1,
			'orderby'          => 'ID',
			'order'            => 'ASC',
			'suppress_filters' => true,
			'no_found_rows'    => true,
		)
	);
	return $posts ? $posts[0] : null;
}

/**
 * Upload directory for sermon audio, created if needed.
 *
 * @return array|WP_Error Array with 'dir' and 'basedir', or an error.
 */
function georg_bridge_upload_dir() {
	$uploads = wp_upload_dir( null, false );
	if ( ! empty( $uploads['error'] ) ) {
		return new WP_Error( 'georg_uploads', $uploads['error'], array( 'status' => 500 ) );
	}
	$dir = trailingslashit( $uploads['basedir'] ) . GEORG_BRIDGE_UPLOAD_SUBDIR;
	if ( ! wp_mkdir_p( $dir ) ) {
		return new WP_Error( 'georg_uploads', __( 'Cannot create the sermons upload directory.', 'georg-bridge' ), array( 'status' => 500 ) );
	}
	return array(
		'dir'     => $dir,
		'basedir' => $uploads['basedir'],
	);
}

/**
 * Whether another attachment already uses uploads/sermons/<filename>.
 *
 * Sharing a file would make the audio URL ambiguous and let deleting one attachment remove
 * the other's file.
 *
 * @param string $filename File name under the sermons directory.
 * @param int    $except   Attachment allowed to use it.
 * @return bool
 */
function georg_bridge_file_in_use( $filename, $except ) {
	$ids = get_posts(
		array(
			'post_type'        => 'attachment',
			'post_status'      => 'any',
			'meta_key'         => '_wp_attached_file', // phpcs:ignore WordPress.DB.SlowDBQuery.slow_db_query_meta_key
			'meta_value'       => GEORG_BRIDGE_UPLOAD_SUBDIR . '/' . $filename, // phpcs:ignore WordPress.DB.SlowDBQuery.slow_db_query_meta_value
			'post__not_in'     => array( (int) $except ),
			'fields'           => 'ids',
			'posts_per_page'   => 1,
			'suppress_filters' => true,
			'no_found_rows'    => true,
		)
	);
	return ! empty( $ids );
}

/**
 * Sanitized .mp3 file name.
 *
 * @param string $name Requested name.
 * @return string
 */
function georg_bridge_filename( $name ) {
	$name = sanitize_file_name( wp_basename( (string) $name ) );
	$name = preg_replace( '/\.[^.]*$/', '', $name );
	if ( '' === $name ) {
		$name = 'sermon';
	}
	return $name . '.mp3';
}

/**
 * Whether a file is an MP3: WordPress' type check accepts any audio/* file whose name ends
 * in .mp3, so the format is also read with getID3.
 *
 * @param string $path     File.
 * @param string $filename Name it will be stored under.
 * @return bool
 */
function georg_bridge_is_mp3( $path, $filename ) {
	$check = wp_check_filetype_and_ext( $path, $filename, array( 'mp3' => 'audio/mpeg' ) );
	if ( 'audio/mpeg' !== $check['type'] || ! filesize( $path ) ) {
		return false;
	}
	require_once ABSPATH . 'wp-admin/includes/media.php';
	$meta = wp_read_audio_metadata( $path );
	return is_array( $meta ) && isset( $meta['dataformat'] ) && 'mp3' === $meta['dataformat'];
}

/**
 * Response describing an audio attachment.
 *
 * @param int  $id      Attachment ID.
 * @param bool $created Whether this request created or changed it.
 * @return WP_REST_Response
 */
function georg_bridge_audio_response( $id, $created ) {
	$response = rest_ensure_response(
		array(
			'id'      => (int) $id,
			'url'     => wp_get_attachment_url( $id ),
			'file'    => get_post_meta( $id, '_wp_attached_file', true ),
			'sha256'  => get_post_meta( $id, GEORG_BRIDGE_SHA256_META, true ),
			'created' => (bool) $created,
		)
	);
	$response->set_status( $created ? 201 : 200 );
	return $response;
}

/**
 * POST /georg/v1/audio: stores an MP3 under uploads/sermons/ as a media-library attachment.
 *
 * The body is the raw MP3 (Content-Type: audio/mpeg), so its hash can be signed;
 * publication_key, filename and title are query parameters.
 *
 * Idempotent by publication key: the same key and content return the existing attachment;
 * the same key with new content stores a new file and points the attachment at it.
 *
 * @param WP_REST_Request $request Request.
 * @return WP_REST_Response|WP_Error
 */
function georg_bridge_upload_audio( $request ) {
	$type = $request->get_content_type();
	if ( ! $type || 'audio/mpeg' !== $type['value'] ) {
		return new WP_Error( 'georg_bad_content_type', __( 'Send the MP3 as the request body with Content-Type: audio/mpeg.', 'georg-bridge' ), array( 'status' => 415 ) );
	}
	$body = (string) $request->get_body();
	if ( '' === $body ) {
		return new WP_Error( 'georg_no_file', __( 'Send the MP3 as the request body.', 'georg-bridge' ), array( 'status' => 400 ) );
	}
	// PHP does not apply its upload limits to raw bodies, so apply WordPress's own.
	if ( strlen( $body ) > wp_max_upload_size() ) {
		return new WP_Error(
			'georg_too_large',
			/* translators: 1: maximum upload size, 2: upload_max_filesize, 3: post_max_size */
			sprintf( __( 'The MP3 is larger than the maximum upload size (%1$s; PHP upload_max_filesize %2$s, post_max_size %3$s).', 'georg-bridge' ), size_format( wp_max_upload_size() ), ini_get( 'upload_max_filesize' ), ini_get( 'post_max_size' ) ),
			array( 'status' => 413 )
		);
	}
	require_once ABSPATH . 'wp-admin/includes/file.php';
	$tmp = wp_tempnam( 'georg-audio.mp3' );
	// phpcs:ignore WordPress.WP.AlternativeFunctions.file_system_operations_file_put_contents
	if ( ! $tmp || strlen( $body ) !== file_put_contents( $tmp, $body ) ) {
		return new WP_Error( 'georg_temp_failed', __( 'Could not store the uploaded file.', 'georg-bridge' ), array( 'status' => 500 ) );
	}
	unset( $body );
	try {
		return georg_bridge_store_audio( $request, $tmp );
	} finally {
		// Not wp_delete_file(): its filter makes media plugins (e.g. WebP Express) log errors
		// for a path outside the uploads directory.
		if ( file_exists( $tmp ) ) {
			unlink( $tmp ); // phpcs:ignore WordPress.WP.AlternativeFunctions.unlink_unlink
		}
	}
}

/**
 * Stores an MP3 written to a temporary file for georg_bridge_upload_audio().
 *
 * @param WP_REST_Request $request Request.
 * @param string          $tmp     Temporary file holding the body.
 * @return WP_REST_Response|WP_Error
 */
function georg_bridge_store_audio( $request, $tmp ) {
	$key      = $request['publication_key'];
	$filename = georg_bridge_filename( $request['filename'] ? $request['filename'] : 'sermon.mp3' );
	if ( ! georg_bridge_is_mp3( $tmp, $filename ) ) {
		return new WP_Error( 'georg_not_mp3', __( 'The upload is not an MP3 file.', 'georg-bridge' ), array( 'status' => 400 ) );
	}
	$sha256 = hash_file( 'sha256', $tmp );
	$title  = $request['title'] ? sanitize_text_field( $request['title'] ) : preg_replace( '/\.mp3$/', '', $filename );

	$lock = georg_bridge_lock( 'audio', $key );
	if ( is_wp_error( $lock ) ) {
		return $lock;
	}
	try {
		$existing = georg_bridge_find( 'attachment', $key, array( 'inherit', 'private' ) );
		if ( $existing ) {
			$current = get_attached_file( $existing->ID );
			if ( get_post_meta( $existing->ID, GEORG_BRIDGE_SHA256_META, true ) === $sha256 && $current && file_exists( $current ) ) {
				if ( $existing->post_title !== $title ) {
					wp_update_post(
						wp_slash(
							array(
								'ID'         => $existing->ID,
								'post_title' => $title,
							)
						)
					);
				}
				return georg_bridge_audio_response( $existing->ID, false );
			}
		}

		$dir = georg_bridge_upload_dir();
		if ( is_wp_error( $dir ) ) {
			return $dir;
		}
		$path = trailingslashit( $dir['dir'] ) . $filename;
		$self = $existing ? $existing->ID : 0;
		if ( file_exists( $path ) && ( hash_file( 'sha256', $path ) !== $sha256 || georg_bridge_file_in_use( $filename, $self ) ) ) {
			$path = trailingslashit( $dir['dir'] ) . wp_unique_filename( $dir['dir'], $filename );
		}
		if ( ! file_exists( $path ) ) {
			// Moving the file directly keeps it under uploads/sermons/ rather than a dated folder.
			if ( ! @rename( $tmp, $path ) ) { // phpcs:ignore WordPress.PHP.NoSilencedErrors.Discouraged, WordPress.WP.AlternativeFunctions.rename_rename
				return new WP_Error( 'georg_move_failed', __( 'Could not store the uploaded file.', 'georg-bridge' ), array( 'status' => 500 ) );
			}
			$stat = stat( dirname( $path ) );
			chmod( $path, $stat['mode'] & 0000666 );
		}

		require_once ABSPATH . 'wp-admin/includes/image.php';
		require_once ABSPATH . 'wp-admin/includes/media.php';

		$url = trailingslashit( wp_upload_dir( null, false )['baseurl'] ) . GEORG_BRIDGE_UPLOAD_SUBDIR . '/' . wp_basename( $path );
		if ( $existing ) {
			$id = $existing->ID;
			update_attached_file( $id, $path );
			wp_update_post(
				wp_slash(
					array(
						'ID'             => $id,
						'post_title'     => $title,
						'post_mime_type' => 'audio/mpeg',
					)
				)
			);
			// wp_update_post() never changes the GUID; podcast feeds may read it, so keep it equal to the file URL.
			global $wpdb;
			$wpdb->update( $wpdb->posts, array( 'guid' => $url ), array( 'ID' => $id ) ); // phpcs:ignore WordPress.DB.DirectDatabaseQuery
			clean_post_cache( $id );
		} else {
			$id = wp_insert_attachment(
				wp_slash(
					array(
						'guid'           => $url,
						'post_mime_type' => 'audio/mpeg',
						'post_title'     => $title,
						'post_content'   => '',
						'post_status'    => 'inherit',
					)
				),
				$path,
				0,
				true
			);
			if ( is_wp_error( $id ) ) {
				$id->add_data( array( 'status' => 500 ) );
				return $id;
			}
			update_post_meta( $id, GEORG_BRIDGE_KEY_META, $key );
		}
		wp_update_attachment_metadata( $id, wp_generate_attachment_metadata( $id, $path ) );
		update_post_meta( $id, GEORG_BRIDGE_SHA256_META, $sha256 );
		return georg_bridge_audio_response( $id, true );
	} finally {
		delete_option( $lock );
	}
}

/**
 * Response describing a sermon post.
 *
 * @param WP_Post $post    Sermon.
 * @param bool    $created Whether this request created it.
 * @return WP_REST_Response
 */
function georg_bridge_sermon_response( $post, $created = false ) {
	$terms = array();
	foreach ( get_object_taxonomies( GEORG_BRIDGE_POST_TYPE ) as $taxonomy ) {
		$names = wp_get_object_terms( $post->ID, $taxonomy, array( 'fields' => 'names' ) );
		if ( ! is_wp_error( $names ) ) {
			$terms[ $taxonomy ] = $names;
		}
	}
	$audio_url = get_post_meta( $post->ID, GEORG_BRIDGE_AUDIO_META, true );
	$response  = rest_ensure_response(
		array(
			'id'       => $post->ID,
			'link'     => get_permalink( $post ),
			'status'   => $post->post_status,
			'title'    => $post->post_title,
			'date'     => mysql_to_rfc3339( $post->post_date ),
			'date_gmt' => '0000-00-00 00:00:00' === $post->post_date_gmt ? null : mysql_to_rfc3339( $post->post_date_gmt ),
			'terms'    => $terms,
			'audio'    => $audio_url ? array(
				'id'  => attachment_url_to_postid( $audio_url ),
				'url' => $audio_url,
			) : null,
			'created'  => (bool) $created,
		)
	);
	$response->set_status( $created ? 201 : 200 );
	return $response;
}

/**
 * Error unless the Church Content sermon post type is registered.
 *
 * @return WP_Error|null
 */
function georg_bridge_require_post_type() {
	if ( post_type_exists( GEORG_BRIDGE_POST_TYPE ) ) {
		return null;
	}
	return new WP_Error( 'georg_no_sermons', __( 'The ctc_sermon post type is not registered; activate Church Content.', 'georg-bridge' ), array( 'status' => 500 ) );
}

/**
 * GET /georg/v1/sermons/<publication_key>.
 *
 * @param WP_REST_Request $request Request.
 * @return WP_REST_Response|WP_Error
 */
function georg_bridge_get_sermon( $request ) {
	$error = georg_bridge_require_post_type();
	if ( $error ) {
		return $error;
	}
	$post = georg_bridge_find( GEORG_BRIDGE_POST_TYPE, $request['publication_key'], georg_bridge_live_statuses() );
	if ( ! $post ) {
		return new WP_Error( 'georg_not_found', __( 'No sermon has this publication key.', 'georg-bridge' ), array( 'status' => 404 ) );
	}
	return georg_bridge_sermon_response( $post );
}

/**
 * Statuses searched for an existing sermon (everything except trash and auto-drafts).
 *
 * @return string[]
 */
function georg_bridge_live_statuses() {
	return array( 'publish', 'future', 'draft', 'pending', 'private' );
}

/**
 * Converts an RFC 3339 date from the request to MySQL format.
 *
 * @param string $value Date such as "2026-11-01T10:30:00".
 * @return string|null
 */
function georg_bridge_mysql_date( $value ) {
	if ( ! $value ) {
		return null;
	}
	$parsed = rest_parse_date( $value, true );
	return $parsed ? gmdate( 'Y-m-d H:i:s', $parsed ) : null;
}

/**
 * Resolves term names to IDs, creating missing terms.
 *
 * @param array $terms Taxonomy => list of names.
 * @return array|WP_Error Taxonomy => list of term IDs.
 */
function georg_bridge_term_ids( $terms ) {
	$result = array();
	foreach ( (array) $terms as $taxonomy => $names ) {
		if ( ! is_object_in_taxonomy( GEORG_BRIDGE_POST_TYPE, $taxonomy ) ) {
			/* translators: %s: taxonomy name */
			return new WP_Error( 'georg_bad_taxonomy', sprintf( __( 'Taxonomy %s is not registered for sermons.', 'georg-bridge' ), $taxonomy ), array( 'status' => 400 ) );
		}
		$ids = array();
		foreach ( (array) $names as $name ) {
			$name = sanitize_text_field( (string) $name );
			if ( '' === $name ) {
				continue;
			}
			$term = term_exists( $name, $taxonomy );
			if ( ! $term ) {
				$term = wp_insert_term( $name, $taxonomy );
				if ( is_wp_error( $term ) ) {
					if ( 'term_exists' !== $term->get_error_code() ) {
						$term->add_data( array( 'status' => 500 ) );
						return $term;
					}
					$term = array( 'term_id' => $term->get_error_data( 'term_exists' ) );
				}
			}
			$ids[] = (int) $term['term_id'];
		}
		$result[ $taxonomy ] = $ids;
	}
	return $result;
}

/**
 * PUT /georg/v1/sermons/<publication_key>: creates or updates the sermon with this key.
 *
 * Taxonomies present in `terms` are replaced (an empty list clears them); others are left alone.
 *
 * @param WP_REST_Request $request Request.
 * @return WP_REST_Response|WP_Error
 */
function georg_bridge_put_sermon( $request ) {
	$error = georg_bridge_require_post_type();
	if ( $error ) {
		return $error;
	}
	$key    = $request['publication_key'];
	$status = $request['status'] ? $request['status'] : 'draft';
	$type   = get_post_type_object( GEORG_BRIDGE_POST_TYPE );
	if ( 'publish' === $status && ! current_user_can( $type->cap->publish_posts ) ) {
		return new WP_Error( 'georg_cannot_publish', __( 'This user cannot publish sermons.', 'georg-bridge' ), array( 'status' => 403 ) );
	}

	$audio_id = $request['audio_attachment_id'];
	if ( null !== $audio_id ) {
		$audio = get_post( (int) $audio_id );
		if ( ! $audio || 'attachment' !== $audio->post_type || 0 !== strpos( (string) $audio->post_mime_type, 'audio/' ) ) {
			return new WP_Error( 'georg_bad_audio', __( 'audio_attachment_id is not an audio attachment.', 'georg-bridge' ), array( 'status' => 400 ) );
		}
	}

	$term_ids = georg_bridge_term_ids( $request['terms'] ? $request['terms'] : array() );
	if ( is_wp_error( $term_ids ) ) {
		return $term_ids;
	}

	$lock = georg_bridge_lock( 'sermon', $key );
	if ( is_wp_error( $lock ) ) {
		return $lock;
	}
	try {
		$existing = georg_bridge_find( GEORG_BRIDGE_POST_TYPE, $key, georg_bridge_live_statuses() );
		if ( $existing && 'draft' === $status && ! in_array( $existing->post_status, array( 'draft', 'pending' ), true ) ) {
			// Someone published (or scheduled, or made private) the sermon after review; an update keeps that.
			$status = $existing->post_status;
		}
		$postarr = array(
			'post_type'   => GEORG_BRIDGE_POST_TYPE,
			'post_title'  => sanitize_text_field( $request['title'] ),
			'post_status' => $status,
		);
		if ( null !== $request['content'] ) {
			$postarr['post_content'] = $request['content'];
		}
		$date     = georg_bridge_mysql_date( $request['date'] );
		$date_gmt = georg_bridge_mysql_date( $request['date_gmt'] );
		if ( $date ) {
			$postarr['post_date'] = $date;
			$postarr['edit_date'] = true;
		}
		if ( $date_gmt ) {
			$postarr['post_date_gmt'] = $date_gmt;
			$postarr['edit_date']     = true;
		}

		if ( $existing ) {
			$postarr['ID'] = $existing->ID;
			$post_id       = wp_update_post( wp_slash( $postarr ), true );
		} else {
			$postarr['meta_input'] = array( GEORG_BRIDGE_KEY_META => $key );
			$post_id               = wp_insert_post( wp_slash( $postarr ), true );
		}
		if ( is_wp_error( $post_id ) ) {
			$post_id->add_data( array( 'status' => 500 ) );
			return $post_id;
		}

		foreach ( $term_ids as $taxonomy => $ids ) {
			$set = wp_set_object_terms( $post_id, $ids, $taxonomy, false );
			if ( is_wp_error( $set ) ) {
				$set->add_data( array( 'status' => 500 ) );
				return $set;
			}
		}

		if ( null !== $audio_id ) {
			update_post_meta( $post_id, GEORG_BRIDGE_AUDIO_META, esc_url_raw( wp_get_attachment_url( (int) $audio_id ) ) );
			wp_update_post(
				array(
					'ID'          => (int) $audio_id,
					'post_parent' => $post_id,
				)
			);
		}

		return georg_bridge_sermon_response( get_post( $post_id ), ! $existing );
	} finally {
		delete_option( $lock );
	}
}
