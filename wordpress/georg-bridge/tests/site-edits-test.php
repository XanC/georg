<?php
// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Command-line tests for the site-edit helpers in includes/sermon.php:
 * `php tests/site-edits-test.php`. Exits non-zero on failure.
 *
 * @package GeorgBridge
 */

require __DIR__ . '/../includes/sermon.php';

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

$georg_fp = georg_bridge_fingerprint( 'Sermon for St. Luke' );
georg_check( 64 === strlen( $georg_fp ) && georg_bridge_fingerprint( 'Sermon for St. Luke' ) === $georg_fp, 'fingerprint is a stable sha256' );
georg_check( georg_bridge_fingerprint( array( 3, 7 ) ) !== georg_bridge_fingerprint( array( 7, 3 ) ), 'term order matters, so callers sort' );

$georg_written = array(
	'title'                 => georg_bridge_fingerprint( 'Sermon for 10/25' ),
	'content'               => georg_bridge_fingerprint( '<p>text</p>' ),
	'terms:ctc_sermon_book' => georg_bridge_fingerprint( array( 1, 2 ) ),
);
$georg_current = array(
	'title'                 => georg_bridge_fingerprint( 'Sermon For 10/25' ),
	'content'               => $georg_written['content'],
	'date'                  => georg_bridge_fingerprint( array( 'x', 'y' ) ),
	'terms:ctc_sermon_book' => georg_bridge_fingerprint( array( 1, 2, 9 ) ),
);
georg_check(
	array( 'terms:ctc_sermon_book', 'title' ) === georg_bridge_changed_fields( $georg_current, $georg_written ),
	'changed fields are the ones whose stored value differs'
);
georg_check( array() === georg_bridge_changed_fields( $georg_written, $georg_written ), 'nothing changed, nothing edited' );

georg_check(
	array( 'title' ) === georg_bridge_conflicts( array( 'title', 'date', 'audio' ), array( 'title', 'terms:ctc_sermon_book' ), array() ),
	'only requested edited fields conflict'
);
georg_check( array() === georg_bridge_conflicts( array( 'title' ), array( 'title' ), array( 'title' ) ), 'overwrite clears a conflict' );
georg_check( array() === georg_bridge_conflicts( array( 'date' ), array( 'title' ), array() ), 'skipping the edited field avoids the conflict' );

// Revisions: bridge user 5, owner user 2.
georg_check( true === georg_bridge_edited_after_georg( array( array( 'id' => 10, 'author' => 5 ), array( 'id' => 12, 'author' => 2 ) ), 5 ), 'a revision by another user after Georg (post 8228) is an edit' );
georg_check( false === georg_bridge_edited_after_georg( array( array( 'id' => 10, 'author' => 5 ), array( 'id' => 11, 'author' => 5 ) ), 5 ), 'only Georg revisions (posts 8212/8216) are not an edit' );
georg_check( false === georg_bridge_edited_after_georg( array( array( 'id' => 14, 'author' => 5 ), array( 'id' => 12, 'author' => 2 ) ), 5 ), 'Georg writing after the edit settles it' );
georg_check( false === georg_bridge_edited_after_georg( array(), 5 ), 'no revisions, no edit' );

$georg_taxonomies = array( 'ctc_sermon_book', 'ctc_sermon_series' );
georg_check( null === georg_bridge_parse_fields( null, $georg_taxonomies ), 'absent fields means all' );
georg_check(
	array( 'title', 'audio', 'terms:ctc_sermon_book' ) === georg_bridge_parse_fields( array( 'title', 'audio', 'terms:ctc_sermon_book', 'title' ), $georg_taxonomies ),
	'known fields are accepted and deduplicated'
);
georg_check( false === georg_bridge_parse_fields( array( 'terms:category' ), $georg_taxonomies ), 'unknown taxonomy is rejected' );
georg_check( false === georg_bridge_parse_fields( array( 'excerpt' ), $georg_taxonomies ), 'unknown field is rejected' );
georg_check( false === georg_bridge_parse_fields( 'title', $georg_taxonomies ), 'a non-list is rejected' );

$georg_next = georg_bridge_next_written( $georg_written, $georg_current, array( 'date', 'terms:ctc_sermon_book' ), false );
georg_check( $georg_written['title'] === $georg_next['title'], 'a skipped edited field keeps the old fingerprint, so it stays edited' );
georg_check( $georg_current['date'] === $georg_next['date'] && $georg_current['terms:ctc_sermon_book'] === $georg_next['terms:ctc_sermon_book'], 'written fields get the read-back fingerprint' );
$georg_legacy = georg_bridge_next_written( array(), $georg_current, array( 'date' ), true );
georg_check( 'edited-on-site' === $georg_legacy['title'] && $georg_current['date'] === $georg_legacy['date'], 'an edited pre-0.4.0 post keeps skipped fields edited' );
$georg_clean = georg_bridge_next_written( array(), $georg_current, array( 'date' ), false );
georg_check( $georg_current['title'] === $georg_clean['title'], 'an unedited pre-0.4.0 post adopts the stored values' );

exit( $georg_failures ? 1 : 0 );
