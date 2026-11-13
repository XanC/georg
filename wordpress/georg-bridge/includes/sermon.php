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

/**
 * For posts written before 0.4.0: whether someone other than the bridge user saved a
 * revision after the bridge user's last one.
 *
 * @param array $revisions   Each array( 'id' => int, 'author' => int ), any order.
 * @param int   $bridge_user The bridge's authenticated user ID.
 * @return bool
 */
function georg_bridge_edited_after_georg( $revisions, $bridge_user ) {
	usort(
		$revisions,
		function ( $a, $b ) {
			return (int) $a['id'] - (int) $b['id'];
		}
	);
	$edited = false;
	foreach ( $revisions as $revision ) {
		$edited = (int) $revision['author'] !== (int) $bridge_user;
	}
	return $edited;
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
