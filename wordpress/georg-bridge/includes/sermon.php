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
