<?php
// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Sermon field helpers without WordPress dependencies.
 *
 * @package GeorgBridge
 */

/**
 * What to do with Church Content's "Full sermon text provided" meta for a request value.
 *
 * @param mixed $value The request's `full_text` (null when absent).
 * @return string|null 'set' (store '1'), 'delete', or null to leave the meta alone.
 */
function georg_bridge_full_text_action( $value ) {
	if ( null === $value ) {
		return null;
	}
	$flag = filter_var( $value, FILTER_VALIDATE_BOOLEAN, FILTER_NULL_ON_FAILURE );
	if ( null === $flag ) {
		return null;
	}
	return $flag ? 'set' : 'delete';
}

/**
 * Fingerprint of a stored field value.
 *
 * @param mixed $value Value as stored (string, or list of term IDs).
 * @return string sha256 hex digest.
 */
function georg_bridge_fingerprint( $value ) {
	return hash( 'sha256', json_encode( $value, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE ) );
}

/**
 * Fields whose current fingerprint differs from the one Georg last wrote.
 *
 * @param array $current Field => fingerprint now.
 * @param array $written Field => fingerprint Georg wrote.
 * @return string[] Sorted field names.
 */
function georg_bridge_changed_fields( $current, $written ) {
	$changed = array();
	foreach ( $written as $field => $hash ) {
		if ( isset( $current[ $field ] ) && $current[ $field ] !== $hash ) {
			$changed[] = $field;
		}
	}
	sort( $changed );
	return $changed;
}

/** Seconds by which a post may be modified after Georg's recorded write time and still
 * count as Georg's own write (the post is saved just before Georg records the time). */
define( 'GEORG_BRIDGE_WRITE_TOLERANCE', 10 );

/**
 * For posts written before 0.4.0: whether someone other than the bridge user saved the
 * post after Georg last wrote it. WordPress's editor sets `_edit_last` on every save but
 * does not always leave a revision, so revisions cannot tell.
 *
 * @param int         $edit_last        The post's `_edit_last` user ID (0 if unset).
 * @param int         $bridge_user      The bridge's authenticated user ID.
 * @param string      $modified_gmt     The post's `post_modified_gmt` (Y-m-d H:i:s, UTC).
 * @param int|null    $georg_written_at When Georg last wrote it (Unix time), or null if
 *                                      Georg does not know; then any non-bridge
 *                                      `_edit_last` counts as an edit.
 * @return bool
 */
function georg_bridge_legacy_edited( $edit_last, $bridge_user, $modified_gmt, $georg_written_at ) {
	if ( ! $edit_last || (int) $edit_last === (int) $bridge_user ) {
		return false;
	}
	if ( null === $georg_written_at ) {
		return true;
	}
	$modified = strtotime( $modified_gmt . ' UTC' );
	return false === $modified || $modified > $georg_written_at + GEORG_BRIDGE_WRITE_TOLERANCE;
}

/**
 * Parses the optional `georg_written_at` parameter.
 *
 * @param mixed $value RFC 3339 time with a zone, or null/empty.
 * @return int|null|false Unix time, null if absent, false if invalid.
 */
function georg_bridge_parse_time( $value ) {
	if ( null === $value || '' === $value ) {
		return null;
	}
	if ( ! is_string( $value ) || ! preg_match( '/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/i', $value ) ) {
		return false;
	}
	$time = strtotime( $value );
	return false === $time ? false : $time;
}

/**
 * Requested fields that were edited on the site and may not be overwritten.
 *
 * @param string[] $requested Fields the request writes.
 * @param string[] $edited    Fields edited on the site.
 * @param string[] $overwrite Fields the request may overwrite anyway.
 * @return string[]
 */
function georg_bridge_conflicts( $requested, $edited, $overwrite ) {
	return array_values( array_diff( array_intersect( $edited, $requested ), $overwrite ) );
}

/**
 * Validates a `fields` or `overwrite` list.
 *
 * @param mixed    $value      Request value (null when absent).
 * @param string[] $taxonomies Taxonomies allowed in "terms:<taxonomy>".
 * @return string[]|null|false List, null when absent, false when invalid.
 */
function georg_bridge_parse_fields( $value, $taxonomies ) {
	if ( null === $value ) {
		return null;
	}
	if ( ! is_array( $value ) ) {
		return false;
	}
	$fields = array();
	foreach ( $value as $field ) {
		if ( ! is_string( $field ) ) {
			return false;
		}
		$known = in_array( $field, array( 'title', 'content', 'date', 'audio', 'full_text' ), true )
			|| ( 0 === strpos( $field, 'terms:' ) && in_array( substr( $field, 6 ), $taxonomies, true ) );
		if ( ! $known ) {
			return false;
		}
		$fields[] = $field;
	}
	return array_values( array_unique( $fields ) );
}

/**
 * The `_georg_written` fingerprints to store after a write.
 *
 * @param array    $previous Fingerprints stored before (empty for posts written before 0.4.0).
 * @param array    $current  Field => fingerprint read back from the saved post.
 * @param string[] $written  Fields this request wrote.
 * @param bool     $legacy_edited Whether a pre-0.4.0 post was edited on the site.
 * @return array
 */
function georg_bridge_next_written( $previous, $current, $written, $legacy_edited ) {
	$next = array();
	foreach ( $current as $field => $hash ) {
		if ( in_array( $field, $written, true ) ) {
			$next[ $field ] = $hash;
		} elseif ( isset( $previous[ $field ] ) ) {
			$next[ $field ] = $previous[ $field ];
		} else {
			// Not written now: an edit made on the site stays an edit.
			$next[ $field ] = $legacy_edited ? 'edited-on-site' : $hash;
		}
	}
	return $next;
}
