<?php
// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Plugin Name: Georg e2e theme support
 * Description: Declares Church Content sermon support the way a ChurchThemes theme does, so the free plugin works with a default theme.
 */

add_action(
	'after_setup_theme',
	function () {
		add_theme_support( 'church-theme-content' );
		add_theme_support(
			'ctc-sermons',
			array(
				'taxonomies'      => array( 'ctc_sermon_topic', 'ctc_sermon_book', 'ctc_sermon_series', 'ctc_sermon_speaker', 'ctc_sermon_tag' ),
				'fields'          => array( '_ctc_sermon_has_full_text', '_ctc_sermon_video', '_ctc_sermon_audio', '_ctc_sermon_pdf' ),
				'field_overrides' => array(),
			)
		);
	},
	11
);
