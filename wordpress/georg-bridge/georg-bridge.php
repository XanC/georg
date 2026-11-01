<?php
// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Plugin Name:       Georg Bridge
 * Plugin URI:        https://github.com/XanC/georg
 * Description:       REST endpoints that let Georg upload sermon audio and create or update Church Content sermons.
 * Version:           0.1.1
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

define( 'GEORG_BRIDGE_NAMESPACE', 'georg/v1' );
define( 'GEORG_BRIDGE_KEY_META', '_georg_publication_key' );
define( 'GEORG_BRIDGE_SHA256_META', '_georg_sha256' );
define( 'GEORG_BRIDGE_POST_TYPE', 'ctc_sermon' );
define( 'GEORG_BRIDGE_AUDIO_META', '_ctc_sermon_audio' );
define( 'GEORG_BRIDGE_UPLOAD_SUBDIR', 'sermons' );
define( 'GEORG_BRIDGE_KEY_PATTERN', '[A-Za-z0-9:._-]{1,190}' );
define( 'GEORG_BRIDGE_LOCK_SECONDS', 300 );

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
			'permission_callback' => 'georg_bridge_permission',
			'args'                => array(
				'publication_key' => array(
					'type'              => 'string',
					'required'          => false,
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
				'permission_callback' => 'georg_bridge_permission',
				'args'                => $key_arg,
			),
			array(
				'methods'             => 'PUT',
				'callback'            => 'georg_bridge_put_sermon',
				'permission_callback' => 'georg_bridge_permission',
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
 * Permission check shared by every route.
 *
 * Requires an Application Password login (not a browser cookie) by a user who can edit
 * other users' posts and upload files. Sites can adjust this with the
 * `georg_bridge_permission` filter.
 *
 * @param WP_REST_Request $request Request.
 * @return bool|WP_Error
 */
function georg_bridge_permission( $request ) {
	$allowed = did_action( 'application_password_did_authenticate' ) > 0
		&& current_user_can( 'edit_others_posts' )
		&& current_user_can( 'upload_files' );

	/**
	 * Filters whether the current user may use the Georg bridge.
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
		__( 'Georg bridge requires an application password for a user who can edit others\' posts and upload files.', 'georg-bridge' ),
		array( 'status' => is_user_logged_in() ? 403 : 401 )
	);
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
 * Error for a missing or oversized upload, or null if the file arrived.
 *
 * PHP drops the whole body when it exceeds post_max_size, and drops the file when it exceeds
 * upload_max_filesize; both are reported as 413 so the limit can be raised.
 *
 * @param array $files Request file params.
 * @return WP_Error|null
 */
function georg_bridge_upload_error( $files ) {
	$length = isset( $_SERVER['CONTENT_LENGTH'] ) ? (int) $_SERVER['CONTENT_LENGTH'] : 0;
	$limit  = wp_convert_hr_to_bytes( (string) ini_get( 'post_max_size' ) );
	if ( empty( $files ) && $limit > 0 && $length > $limit ) {
		/* translators: %s: PHP post_max_size setting */
		return new WP_Error( 'georg_too_large', sprintf( __( 'The request is larger than PHP post_max_size (%s).', 'georg-bridge' ), ini_get( 'post_max_size' ) ), array( 'status' => 413 ) );
	}
	$error = empty( $files['file']['error'] ) ? UPLOAD_ERR_OK : (int) $files['file']['error'];
	if ( UPLOAD_ERR_INI_SIZE === $error || UPLOAD_ERR_FORM_SIZE === $error ) {
		/* translators: %s: PHP upload_max_filesize setting */
		return new WP_Error( 'georg_too_large', sprintf( __( 'The file is larger than PHP upload_max_filesize (%s).', 'georg-bridge' ), ini_get( 'upload_max_filesize' ) ), array( 'status' => 413 ) );
	}
	if ( empty( $files['file'] ) || UPLOAD_ERR_OK !== $error || empty( $files['file']['tmp_name'] ) ) {
		return new WP_Error( 'georg_no_file', __( 'Send the MP3 as the multipart field "file".', 'georg-bridge' ), array( 'status' => 400 ) );
	}
	return null;
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
 * Idempotent by publication key: the same key and content return the existing attachment;
 * the same key with new content stores a new file and points the attachment at it.
 *
 * @param WP_REST_Request $request Request.
 * @return WP_REST_Response|WP_Error
 */
function georg_bridge_upload_audio( $request ) {
	$files = $request->get_file_params();
	$error = georg_bridge_upload_error( $files );
	if ( $error ) {
		return $error;
	}
	$key = $request['publication_key'];
	if ( ! georg_bridge_valid_key( $key ) ) {
		return new WP_Error( 'rest_missing_callback_param', __( 'Missing parameter(s): publication_key', 'georg-bridge' ), array( 'status' => 400 ) );
	}
	$tmp      = $files['file']['tmp_name'];
	$filename = georg_bridge_filename( $request['filename'] ? $request['filename'] : $files['file']['name'] );
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
			// Moving the PHP upload directly keeps the file under uploads/sermons/ rather than a dated folder.
			if ( ! @move_uploaded_file( $tmp, $path ) ) { // phpcs:ignore WordPress.PHP.NoSilencedErrors.Discouraged
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
